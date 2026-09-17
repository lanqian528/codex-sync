"""Personal Codex provider synchronizer. Never accesses login credentials."""
from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import warnings
from urllib.parse import urlsplit, urlunsplit

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
def lock(home, name=".codex-sync.lock"):
    path = home / name
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


def cloud_url(value):
    """Recognize a homepage/domain without probing other hosts or endpoints."""
    if not isinstance(value, str):
        raise SafeError("Invalid cloud URL.")
    value = value.strip()
    if not value or "\\" in value:
        raise SafeError("Invalid cloud URL.")
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value
    https_url(value)
    parsed = urlsplit(value)
    path = parsed.path
    if path in ("", "/", "/index.html"):
        path = "/config.json"
    # Explicit custom endpoints remain untouched. No redirects or URL scanning.
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def cloud_origin(value):
    parsed = urlsplit(cloud_url(value))
    return parsed.hostname.lower(), parsed.port or 443


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
    url = cloud_url(connection["url"])
    if "access_key" in connection:
        key = connection["access_key"]
        if not isinstance(key, str) or not re.fullmatch(r"[!-~]{32,256}", key):
            raise SafeError("Access key must contain 32-256 printable non-space characters.")
        authorization = "Bearer " + key
    else:
        auth = base64.b64encode((connection["username"] + ":" + connection["password"]).encode()).decode()
        authorization = "Basic " + auth
    req = urllib.request.Request(url, headers={
        "Authorization": authorization, "Cache-Control": "no-store", "Accept": "application/json"})
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


def apply(home, cloud):
    validate_cloud(cloud)
    home = Path(home).expanduser().absolute()
    private(home)
    if not home.is_dir():
        raise SafeError("Codex directory must already exist; initialize Pro first.")
    with lock(home):
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
            atomic_write(state_path, json.dumps(state).encode(), state_snapshot)
        if output == old:
            return "unchanged"
        atomic_write(path, output, original)
        return "updated; reopen Codex and start a new session"


def connection_path():
    default = Path.home() / ".config" / "codex-sync" / "connection.json"
    return Path(os.environ.get("CODEX_SYNC_CONFIG", str(default))).expanduser().absolute()


def read_connection(path, allow_missing=False, check_url=True):
    private(path.parent)
    private(path)
    previous = snapshot(path)
    data = previous[0]
    if data is None:
        if allow_missing:
            return {}, previous
        raise SafeError("Local connection.json is missing.")
    c = parse_json(data)
    if not isinstance(c, dict) or "url" not in c or set(c) - {"url", "access_key", "username", "password", "codex_home"}:
        raise SafeError("Invalid local connection configuration.")
    if "access_key" in c:
        if "username" in c or "password" in c:
            raise SafeError("Use access_key or legacy credentials, not both.")
    elif not {"username", "password"} <= set(c) or any(not isinstance(c[k], str) or not c[k] or any(ord(x) < 32 for x in c[k]) for k in ("username", "password")) or ":" in c["username"]:
        raise SafeError("Access key or valid legacy credentials required.")
    if check_url:
        cloud_url(c["url"])
    return c, previous


def runtime_path(suffix="status"):
    path = connection_path()
    return path.with_name(path.stem + "." + suffix + ".json")


def record_runtime(message, mode=None):
    """Best-effort local diagnostics. Never records credentials or raw exceptions."""
    path = runtime_path()
    try:
        private(path.parent)
        if not path.parent.is_dir():
            return
        private(path)
        data = {"checked_at": time.time(), "message": message, "cloud_mode": mode}
        atomic_write(path, json.dumps(data).encode(), snapshot(path))
    except Exception:
        pass  # A status-file failure cannot undo or mask a completed sync.


def sync():
    mode = None
    try:
        result, mode = sync_once()
        record_runtime(result, mode)
        return result
    except SafeError as error:
        record_runtime(str(error), getattr(error, "cloud_mode", None))
        raise
    except Exception:
        record_runtime("Operation failed safely; existing configuration retained.")
        raise


def sync_once():
    path = connection_path()
    private(path.parent)
    if not path.parent.is_dir():
        raise SafeError("Local connection.json is missing.")
    # Editing connection settings cannot race an in-flight synchronization.
    with lock(path.parent, ".codex-sync-connection.lock"):
        c, _ = read_connection(path)
        home = c.get("codex_home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        cloud = fetch(c)
        try:
            return apply(home, cloud), cloud["mode"]
        except SafeError as error:
            error.cloud_mode = cloud["mode"]
            raise


@contextlib.contextmanager
def watch_guard():
    directory = connection_path().parent
    private(directory)
    if not directory.is_dir():
        raise SafeError("Run codex-sync config before starting watch.")
    with lock(directory, connection_path().stem + ".watch.lock"):
        path = runtime_path("watch")
        private(path)
        identity = json.dumps({"pid": os.getpid(), "created_at": psutil.Process().create_time()}).encode()
        atomic_write(path, identity, snapshot(path))
        try:
            yield
        finally:
            try:
                if snapshot(path)[0] == identity:
                    path.unlink()
            except Exception:
                pass


def installed_binary():
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "codex-sync" / "codex-sync.exe"
    return Path.home() / ".local" / "share" / "codex-sync" / "codex-sync"


def watcher_process():
    """Validate identity before ever stopping a process; never targets Codex."""
    path = runtime_path("watch")
    private(path)
    raw, _ = snapshot(path)
    if raw is None:
        return None
    identity = parse_json(raw)
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int or not isinstance(identity.get("created_at"), (int, float)):
        raise SafeError("Cannot confirm the monitor process identity.")
    try:
        process = psutil.Process(identity["pid"])
        if abs(process.create_time() - identity["created_at"]) > 0.01 or process.username() != psutil.Process().username():
            return None
        args = process.cmdline()
        if "watch" not in args[1:]:
            return None
        executable = Path(process.exe()).resolve()
        allowed = {installed_binary().resolve()}
        if getattr(sys, "frozen", False):
            allowed.add(Path(sys.executable).resolve())
        if executable in allowed:
            return process
        if not getattr(sys, "frozen", False) and executable == Path(sys.executable).resolve():
            cwd = Path(process.cwd())
            if any((cwd / arg).resolve() == Path(__file__).resolve() for arg in args[1:] if not arg.startswith("-")):
                return process
        return None
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, OSError):
        raise SafeError("Cannot confirm the monitor process identity.") from None


WINDOWS_SERVICE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $job = Get-ScheduledTask -TaskName CodexSync -ErrorAction SilentlyContinue
    if (-not $job) { @{installed=$false;running=$false;enabled=$false} | ConvertTo-Json -Compress; exit 0 }
    $launcher = Join-Path $env:LOCALAPPDATA 'codex-sync\watch.vbs'
    $hostExe = Join-Path $env:WINDIR 'System32\wscript.exe'
    if (@($job.Actions).Count -ne 1 -or $job.Actions[0].Execute -ine $hostExe -or $job.Actions[0].Arguments.Trim() -ine ('"' + $launcher + '"')) {
        throw 'Unexpected task action'
    }
    if ($env:CODEX_SYNC_SERVICE_ACTION -eq 'start') {
        Enable-ScheduledTask -TaskName CodexSync | Out-Null
        Start-ScheduledTask -TaskName CodexSync
    } elseif ($env:CODEX_SYNC_SERVICE_ACTION -eq 'stop') {
        Disable-ScheduledTask -TaskName CodexSync | Out-Null
        Stop-ScheduledTask -TaskName CodexSync
    }
    $job = Get-ScheduledTask -TaskName CodexSync
    @{installed=$true;running=($job.State -eq 'Running');enabled=([string]$job.State -ne 'Disabled')} | ConvertTo-Json -Compress
} catch {
    Write-Output '{"error":"service_unavailable"}'
    exit 1
}
'''


def service_call(action="status"):
    if action not in ("status", "start", "stop"):
        raise SafeError("Invalid monitor action.")
    default = (Path.home() / ".config" / "codex-sync" / "connection.json").absolute()
    if os.path.normcase(str(connection_path())) != os.path.normcase(str(default)):
        raise SafeError("Background service controls require the default connection file.")
    kwargs = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 15}
    try:
        if os.name == "nt":
            powershell = str(Path(os.environ["WINDIR"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SERVICE_SCRIPT],
                                    env=dict(os.environ, CODEX_SYNC_SERVICE_ACTION=action),
                                    creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
            if result.returncode:
                raise SafeError("Cannot manage the installed monitor task.")
            state = parse_json(result.stdout)
            if not isinstance(state, dict) or set(state) != {"installed", "running", "enabled"} or any(type(v) is not bool for v in state.values()):
                raise SafeError("Cannot read the installed monitor task status.")
            return state
        if sys.platform.startswith("linux"):
            unit = Path.home() / ".config" / "systemd" / "user" / "codex-sync.service"
            no_links(unit)
            if not unit.is_file():
                return {"installed": False, "running": False, "enabled": False}
            if action != "status":
                # Scope service actions to the unit generated by our installer.
                expected = 'ExecStart="{}" watch'.format(installed_binary())
                if expected not in unit.read_text():
                    raise SafeError("Unexpected monitor service definition; refusing changes.")
                verb = "enable" if action == "start" else "disable"
                result = subprocess.run(["systemctl", "--user", verb, "--now", "codex-sync.service"], **kwargs)
                if result.returncode:
                    raise SafeError("Cannot manage the installed monitor service.")
            result = subprocess.run(["systemctl", "--user", "show", "codex-sync.service", "--property=LoadState,ActiveState,UnitFileState", "--no-pager"], **kwargs)
            if result.returncode:
                raise SafeError("Cannot read the installed monitor service status.")
            fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            return {"installed": fields.get("LoadState") == "loaded", "running": fields.get("ActiveState") in ("active", "activating"), "enabled": fields.get("UnitFileState") in ("enabled", "enabled-runtime")}
        raise SafeError("Background service controls are supported on Windows and Linux.")
    except SafeError:
        raise
    except Exception:
        raise SafeError("Background service is unavailable; no Codex settings were changed.") from None


def control_monitor(action):
    state = service_call("status")
    if not state["installed"]:
        raise SafeError("Monitor service is not installed; run the installation script first.")
    if action == "start":
        read_connection(connection_path())
    identity = watcher_process() if action == "stop" else None
    result = service_call(action)
    if action == "stop" and identity is not None:
        # Task Scheduler may stop WScript but leave its console child alive.
        current = watcher_process()
        if current is not None and current.pid == identity.pid:
            try:
                current.terminate()
                current.wait(timeout=3)
            except psutil.NoSuchProcess:
                pass
            except psutil.TimeoutExpired:
                current.kill()
                current.wait(timeout=3)
    return "已请求启动后台监控，自启动已开启。" if action == "start" else "后台监控已停止，自启动已关闭。"


def show_status():
    print("\nCodex Sync · 客户端状态", flush=True)
    try:
        state = service_call()
        process = watcher_process()
        running = state["running"] or process is not None
        print("监控服务：" + ("运行中" if running else "已停止" if state["installed"] else "未安装"))
        print("开机/登录自启动：" + ("已开启" if state["enabled"] else "已关闭"))
    except Exception:
        print("监控服务：状态无法确认（自定义配置请自行管理对应进程）")
    try:
        connection, _ = read_connection(connection_path())
        print("云端地址：" + cloud_url(connection["url"]))
        home = Path(connection.get("codex_home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")).expanduser().absolute()
        print("Codex 目录：" + str(home))
        config = home / "config.toml"
        private(config)
        raw, _ = snapshot(config)
        provider = tomlkit.parse((raw or b"").decode()).get("model_provider", "openai")
        label = "Pro / OpenAI" if provider == "openai" else "第三方 API" if provider == PROVIDER else "其他 provider"
        print("本地用户配置：" + label)
    except Exception:
        print("本地连接或 provider：未配置或无法读取，请选择修改配置。")
    try:
        path = runtime_path()
        private(path)
        raw, _ = snapshot(path)
        if raw is None:
            raise ValueError()
        status = parse_json(raw)
        checked = float(status["checked_at"])
        if not 0 < checked <= time.time() + 60:
            raise ValueError()
        print("最近检查：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(checked)))
        mode = status.get("cloud_mode")
        print("上次云端模式：" + ({"pro": "Pro", "api": "API"}.get(mode, "未能读取")))
        descriptions = {
            "unchanged": "配置已一致，无需修改。",
            "updated; reopen Codex and start a new session": "配置已更新，请重新打开 Codex 并使用新会话。",
            "Codex is running; waiting until it exits.": "这是旧版的等待记录；新版同步会直接更新配置文件。",
            "Cannot confirm Codex process state; deferred.": "这是旧版的进程检查记录；新版同步不再等待进程退出。",
            "Cloud fetch failed; existing configuration retained.": "云端连接失败，保留原配置。",
        }
        print("同步结果：" + descriptions.get(status.get("message"), "同步未完成；运行 codex-sync sync 可查看具体原因。"))
        if time.time() - checked > 150:
            print("提示：以上是历史记录，超过 150 秒未刷新。")
    except Exception:
        print("最近检查：暂无记录。")
    print("状态仅反映后台监控和用户配置，运行中的 Codex 会话不一定已切换。")


def terminal_menu():
    while True:
        show_status()
        if not sys.stdin.isatty():
            return 0
        print("\n1. 启动监控（开启自启动）\n2. 停止监控（关闭自启动）\n3. 修改连接配置\n4. 同步一次\n5. 刷新状态\n0. 退出菜单（后台继续运行）")
        try:
            choice = input("请选择 [0-5]：").strip()
            if choice in ("0", "q", "quit", "exit"):
                return 0
            if choice == "1":
                print(control_monitor("start"))
            elif choice == "2":
                print(control_monitor("stop"))
            elif choice == "3":
                print(configure(argparse.Namespace(url=None, codex_home=None, set_key=False)))
            elif choice == "4":
                print(sync())
            elif choice not in ("", "5"):
                print("请输入 0 到 5。")
        except EOFError:
            return 0
        except SafeError as error:
            print(str(error))
        except Exception:
            print("操作未完成，原配置保留。")


def secret_input(prompt):
    if not sys.stdin.isatty():
        raise SafeError("An interactive terminal is required to enter a key securely.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SafeError("Cannot hide key input; settings were not changed.") from None


def configure(args):
    path = connection_path()
    c, previous = read_connection(path, allow_missing=True, check_url=False)
    candidate = dict(c)
    try:
        old_url = cloud_url(c["url"]) if c else None
    except SafeError:
        old_url = None
    interactive = not c or not (args.url is not None or args.codex_home is not None or args.set_key)
    if interactive and not sys.stdin.isatty():
        raise SafeError("Run config in an interactive terminal, or supply --url / --codex-home for existing settings.")
    url = args.url
    if url is None and interactive:
        url = input("Cloud URL or domain [{}]: ".format(old_url or "required")) or old_url
    candidate["url"] = cloud_url(url if url is not None else old_url)
    host_changed = bool(c) and (old_url is None or cloud_origin(candidate["url"]) != cloud_origin(old_url))
    need_key = args.set_key or interactive or host_changed or not c
    if need_key:
        if host_changed:
            print("Cloud host changed. Enter the key for the new host; the old key will not be sent automatically.")
        keep = bool(c) and not host_changed
        key = secret_input("ACCESS_KEY (hidden{}): ".format("; Enter keeps the current key" if keep else "; required"))
        if key:
            candidate.pop("username", None)
            candidate.pop("password", None)
            candidate["access_key"] = key
        elif not keep:
            raise SafeError("A key is required; settings were not changed.")
    home = c.get("codex_home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    if args.codex_home is not None:
        home = args.codex_home
    elif interactive:
        home = input("Codex directory [{}]: ".format(home)) or home
    if not isinstance(home, str) or not home or any(ord(x) < 32 for x in home):
        raise SafeError("Invalid Codex directory.")
    home_path = Path(home).expanduser().absolute()
    private(home_path)
    if not home_path.is_dir():
        raise SafeError("Codex directory must already exist; initialize Pro first.")
    private(home_path / "config.toml")  # Permissions only; never reads its contents.
    candidate["codex_home"] = str(home_path)
    no_links(path.parent)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
        restrict(path.parent)
    private(path.parent)
    with lock(path.parent, ".codex-sync-connection.lock"):
        if snapshot(path) != previous:
            raise SafeError("Connection settings changed externally; retry config.")
        # Only authenticate and validate a read. Never applies provider settings.
        fetch(candidate)
        if candidate != c:
            atomic_write(path, (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode(), previous)
    return "Connection verified and saved. Watch will reload it next cycle; Codex config.toml was not modified."


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("sync", "watch", "config", "status", "start", "stop"), help="省略时打开状态和操作菜单")
    parser.add_argument("--url", help="config: homepage, domain, or full config URL")
    parser.add_argument("--codex-home", help="config: change the Codex directory")
    parser.add_argument("--set-key", action="store_true", help="config: securely prompt for a new access key")
    args = parser.parse_args()
    if args.command != "config" and (args.url is not None or args.codex_home is not None or args.set_key):
        parser.error("configuration options require the config command")
    if args.command is None:
        return terminal_menu()
    if args.command == "status":
        show_status()
        return 0
    guard = watch_guard() if args.command == "watch" else contextlib.nullcontext()
    try:
        with guard:
            return command_loop(args)
    except SafeError as error:
        print(str(error), flush=True)
        return 1


def command_loop(args):
    last = None
    while True:
        code = 0
        try:
            if args.command == "config":
                message = configure(args)
            elif args.command in ("start", "stop"):
                message = control_monitor(args.command)
            else:
                message = sync()
        except SafeError as exc:
            code, message = 1, str(exc)
        except Exception:
            code, message = 1, "Operation failed safely; existing configuration retained."
        if message != last:
            print(message, flush=True)
            last = message
        if args.command != "watch":
            return code
        time.sleep(60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
