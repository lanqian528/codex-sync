"""Personal Codex provider synchronizer. Never accesses login credentials."""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

import psutil
import tomlkit

PROVIDER = "synced_api"
LIMIT = 65536


class SafeError(Exception):
    """Only constant, secret-free messages may be passed here."""


def no_links(path):
    for p in (path, *path.parents):
        if p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 1024):
            raise SafeError("Symlink/reparse paths are not supported.")


def win_security():
    import win32api
    import win32security as ws
    token = ws.OpenProcessToken(win32api.GetCurrentProcess(), 8)
    return ws, ws.GetTokenInformation(token, ws.TokenUser)[0]


def restrict(path):
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    ws, sid = win_security()
    acl = ws.ACL()
    flags = 3 if path.is_dir() else 0
    for principal in (sid, ws.ConvertStringSidToSid("S-1-5-18")):
        acl.AddAccessAllowedAceEx(ws.ACL_REVISION, flags, 0x1F01FF, principal)
    ws.SetNamedSecurityInfo(str(path), ws.SE_FILE_OBJECT,
                            ws.DACL_SECURITY_INFORMATION | ws.PROTECTED_DACL_SECURITY_INFORMATION,
                            None, None, acl, None)


def private(path):
    no_links(path)
    if not path.exists():
        return
    if os.name != "nt":
        st = path.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise SafeError("Private file/directory permissions required (owner only).")
    else:
        ws, sid = win_security()
        sd = ws.GetNamedSecurityInfo(str(path), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
        acl = sd.GetSecurityDescriptorDacl()
        allowed = {str(sid), str(ws.ConvertStringSidToSid("S-1-5-18"))}
        if acl is None:
            raise SafeError("Private Windows ACL required.")
        for i in range(acl.GetAceCount()):
            ace = acl.GetAce(i)
            if ace[0][0] != 1 and ace[1] and str(ace[-1]) not in allowed:
                raise SafeError("Private Windows ACL required (user and SYSTEM only).")


def snapshot(path):
    no_links(path)
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 2 * 1024 * 1024:
            raise SafeError("Unsafe or oversized local file.")
        data = path.read_bytes()
        return data, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except FileNotFoundError:
        return None, None


def atomic_write(path, data, expected, before_commit=lambda: None):
    no_links(path)
    fd, name = tempfile.mkstemp(prefix=".codex-sync-", dir=path.parent)
    tmp = Path(name)
    try:
        os.close(fd)
        restrict(tmp)  # Secure before writing any secret bytes.
        with tmp.open("wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if tmp.read_bytes() != data:
            raise SafeError("Temporary file verification failed.")
        before_commit()
        if snapshot(path) != expected:
            raise SafeError("File changed externally; retry after closing editors.")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def lock(home):
    path = home / ".codex-sync.lock"
    no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        restrict(path)
        with os.fdopen(fd, "r+b", closefd=False) as f:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    f.write(b"0")
                    f.flush()
                f.seek(0)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise SafeError("Another synchronizer holds the lock.") from None
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise SafeError("Another synchronizer holds the lock.") from None
            yield
    finally:
        os.close(fd)


def https_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise SafeError("Invalid HTTPS URL.")
    try:
        p = urlsplit(value)
        if p.scheme != "https" or not p.hostname or p.username is not None or p.password is not None or p.fragment or p.query:
            raise ValueError()
        _ = p.port
    except ValueError:
        raise SafeError("HTTPS URL without credentials/query/fragment required.") from None
    return value


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise SafeError("Duplicate JSON field.")
        result[key] = value
    return result


def parse_json(data):
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise SafeError("Invalid JSON.") from None


def validate_cloud(obj):
    if not isinstance(obj, dict) or set(obj) != {"mode", "base_url", "api_key"}:
        raise SafeError("Cloud JSON must contain exactly mode/base_url/api_key.")
    if obj["mode"] not in ("api", "pro"):
        raise SafeError("Invalid mode.")
    https_url(obj["base_url"])
    key = obj["api_key"]
    if not isinstance(key, str) or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise SafeError("Invalid API key.")
    if obj["mode"] == "api" and (not key or key.startswith("REPLACE_")):
        raise SafeError("API mode requires a real nonempty key.")
    return obj


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SafeError("Cloud redirects are forbidden.")


def fetch(connection):
    https_url(connection["url"])
    auth = base64.b64encode((connection["username"] + ":" + connection["password"]).encode()).decode()
    req = urllib.request.Request(connection["url"], headers={
        "Authorization": "Basic " + auth, "Cache-Control": "no-store", "Accept": "application/json"})
    try:
        # Do not inherit ambient proxy configuration that could disclose credentials.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=15) as response:
            if response.status != 200:
                raise SafeError("Cloud returned a non-success response.")
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise SafeError("Cloud response too large.")
        return validate_cloud(parse_json(data))
    except SafeError:
        raise
    except Exception:
        raise SafeError("Cloud fetch failed; existing configuration retained.") from None


def ensure_idle():
    try:
        me = psutil.Process().username()
        for proc in psutil.process_iter():
            try:
                if proc.pid == os.getpid():
                    continue
                name = proc.name().lower()
                # Native Codex App, CLI, helpers/backends. Node wrappers checked below.
                candidate = "codex" in name and not name.startswith(("codex-sync", "codex_sync"))
                if not candidate and name not in ("node", "node.exe", "electron", "electron.exe"):
                    continue
                if proc.username() != me:
                    continue
                if candidate or any(re.search(r"(^|[/\\@])codex([/\\.\s-]|$)", arg, re.I) for arg in proc.cmdline()):
                    raise SafeError("Codex is running; waiting until it exits.")
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                raise SafeError("Cannot confirm Codex process state; deferred.") from None
    except SafeError:
        raise
    except Exception:
        raise SafeError("Cannot confirm Codex process state; deferred.") from None


def apply(home, cloud, idle=ensure_idle):
    validate_cloud(cloud)
    home = Path(home).expanduser().absolute()
    private(home)
    if not home.is_dir():
        raise SafeError("Codex directory must already exist; initialize Pro first.")
    with lock(home):
        idle()
        path = home / "config.toml"
        state_path = home / ".codex-sync-state.json"
        private(path)
        original = snapshot(path)
        old = original[0] or b""
        try:
            doc = tomlkit.parse(old.decode("utf-8"))
        except Exception:
            raise SafeError("Invalid TOML; configuration retained.") from None
        providers = doc.get("model_providers", {})
        if not hasattr(providers, "keys"):
            raise SafeError("Invalid providers table.")
        if doc.get("profile") or any("model_provider" in p or "model_providers" in p for p in doc.get("profiles", {}).values()):
            raise SafeError("Profile provider override detected; resolve it manually.")
        if (home / "requirements.toml").exists() or (home / "managed_config.toml").exists():
            raise SafeError("Managed policy detected; automatic switching disabled.")
        state_snapshot = snapshot(state_path)
        if state_snapshot[0] is None:
            if doc.get("model_provider", "openai") != "openai" or PROVIDER in providers:
                raise SafeError("Initial provider/table conflict; no takeover performed.")
            state = {"present": "model_provider" in doc, "provider": doc.get("model_provider", "openai")}
        else:
            private(state_path)
            state = parse_json(state_snapshot[0])
            if not isinstance(state, dict) or set(state) != {"present", "provider"} or type(state["present"]) is not bool or state["provider"] != "openai":
                raise SafeError("Invalid original-provider record; refusing changes.")
            if doc.get("model_provider", "openai") not in ("openai", PROVIDER):
                raise SafeError("Provider changed externally; refusing changes.")
        if PROVIDER in providers:
            table = providers[PROVIDER]
            expected_keys = {"name", "base_url", "wire_api", "requires_openai_auth", "experimental_bearer_token"}
            if set(table) != expected_keys or table.get("name") != "Synced API" or table.get("wire_api") != "responses" or table.get("requires_openai_auth") != True:
                raise SafeError("Dedicated provider table conflict; refusing changes.")
        if cloud["mode"] == "api":
            doc["model_provider"] = PROVIDER
            if "model_providers" not in doc:
                doc["model_providers"] = tomlkit.table()
            if PROVIDER not in doc["model_providers"]:
                doc["model_providers"][PROVIDER] = tomlkit.table()
            table = doc["model_providers"][PROVIDER]
            for key, value in {"name": "Synced API", "base_url": cloud["base_url"], "wire_api": "responses", "requires_openai_auth": True, "experimental_bearer_token": cloud["api_key"]}.items():
                table[key] = value
        else:
            if state["present"]:
                doc["model_provider"] = state["provider"]
            else:
                doc.pop("model_provider", None)
            if PROVIDER in providers:
                del doc["model_providers"][PROVIDER]
        output = tomlkit.dumps(doc).encode("utf-8")
        tomlkit.parse(output.decode())
        if state_snapshot[0] is None:
            if snapshot(path) != original:
                raise SafeError("File changed externally; deferred.")
            atomic_write(state_path, json.dumps(state).encode(), state_snapshot, idle)
        if output == old:
            return "unchanged"
        atomic_write(path, output, original, idle)
        return "updated; reopen Codex and start a new session"


def connection_path():
    default = Path.home() / ".config" / "codex-sync" / "connection.json"
    return Path(os.environ.get("CODEX_SYNC_CONFIG", str(default))).expanduser().absolute()


def sync():
    path = connection_path()
    private(path.parent)
    private(path)
    data, _ = snapshot(path)
    if data is None:
        raise SafeError("Local connection.json is missing.")
    c = parse_json(data)
    if not isinstance(c, dict) or not {"url", "username", "password"} <= set(c) or set(c) - {"url", "username", "password", "codex_home"}:
        raise SafeError("Invalid local connection configuration.")
    if any(not isinstance(c[k], str) or not c[k] or any(ord(x) < 32 for x in c[k]) for k in ("username", "password")) or ":" in c["username"]:
        raise SafeError("Invalid read credentials.")
    home = c.get("codex_home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return apply(home, fetch(c))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "watch"))
    args = parser.parse_args()
    last = None
    while True:
        code = 0
        try:
            message = sync()
        except SafeError as exc:
            code, message = 1, str(exc)
        except Exception:
            code, message = 1, "Synchronization failed safely; configuration retained."
        if message != last:
            print(message, flush=True)
            last = message
        if args.command == "sync":
            return code
        time.sleep(60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
