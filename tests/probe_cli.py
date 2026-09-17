"""Optional real CLI probe: only fake credentials and a temporary CODEX_HOME.

Run: python tests/probe_cli.py /absolute/path/to/codex[.exe]
The loopback HTTP fixture is intentionally NOT a production cloud URL.
"""
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_sync as s


def main():
    cli = sys.argv[1]
    observed = []
    ready = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            observed.append(dict(self.headers))
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"intentional fake-key probe"}}')
            ready.set()
        def log_message(self, *args):
            pass
    with tempfile.TemporaryDirectory(prefix='codex-sync-probe-') as tmp:
        home = Path(tmp)
        s.restrict(home)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('CODEX', 'OPENAI'))}
        env.update(CODEX_HOME=tmp, HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9', NO_PROXY='127.0.0.1,localhost', OTEL_SDK_DISABLED='true')
        version = subprocess.run([cli, '--version'], env=env, cwd=tmp, capture_output=True, text=True, timeout=15).stdout.strip()
        enc = lambda v: base64.urlsafe_b64encode(json.dumps(v).encode()).decode().rstrip('=')
        jwt = enc({'alg':'none'}) + '.' + enc({'exp':4102444800,'https://api.openai.com/auth':{'chatgpt_account_id':'fake-account','chatgpt_plan_type':'pro'}}) + '.fake'
        fake_auth = {'auth_mode':'chatgpt', 'OPENAI_API_KEY':None, 'tokens':{'id_token':jwt,'access_token':'FAKE-PRO-TOKEN-MUST-NOT-LEAK','refresh_token':'FAKE-REFRESH','account_id':'fake-account'}, 'last_refresh':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}
        auth = home / 'auth.json'
        auth.write_text(json.dumps(fake_auth))
        s.restrict(auth)
        auth_before = auth.read_bytes()
        s.apply(home, {'mode':'api','base_url':'https://example.invalid/v1','api_key':'FAKE-API-KEY-EXPECTED'})
        config = home / 'config.toml'
        config.write_text(config.read_text().replace('https://example.invalid/v1', f'http://127.0.0.1:{server.server_port}/v1'))
        with (home / 'cli-output.txt').open('wb') as output:
            proc = subprocess.Popen([cli,'exec','--skip-git-repo-check','--sandbox','read-only','-m','gpt-5.4','Reply OK without tools.'], cwd=tmp, env=env, stdout=output, stderr=output)
            try:
                got_request = ready.wait(45)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                server.shutdown()
                server.server_close()
        if not got_request:
            print('FAIL: no API request observed; version:', version)
            # Only fake data exists in this isolated fixture.
            print((home / 'cli-output.txt').read_text(errors='replace')[-3000:])
            return 1
        normalized = [{k.lower():v for k,v in h.items()} for h in observed]
        assert all(h.get('authorization') == 'Bearer FAKE-API-KEY-EXPECTED' for h in normalized)
        assert all('FAKE-PRO-TOKEN' not in json.dumps(h) and 'chatgpt-account-id' not in h for h in normalized)
        assert auth.read_bytes() == auth_before
        print(json.dumps({'version':version,'platform':sys.platform,'requests':len(observed),'api_bearer_verified':True,'pro_token_absent':True,'auth_unchanged':True}))
        return 0


if __name__ == '__main__':
    sys.exit(main())
