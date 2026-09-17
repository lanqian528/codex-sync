"""Check frozen dependencies and fail-closed network behavior with fake configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_sync as s

with tempfile.TemporaryDirectory(prefix='codex-sync-binary-') as directory:
    root = Path(directory)
    s.restrict(root)
    config = root / 'connection.json'
    config.write_text(json.dumps({'url':'https://127.0.0.1:1/config.json','username':'fake-reader','password':'fake-read-password','codex_home':directory}))
    s.restrict(config)
    target = root / 'config.toml'
    target.write_text('# untouched\nmodel="fake"\n')
    s.restrict(target)
    before = s.snapshot(target)
    result = subprocess.run([str(Path(sys.argv[1]).resolve()), 'sync'], env=dict(os.environ,CODEX_SYNC_CONFIG=str(config)), capture_output=True, text=True, timeout=25)
    assert result.returncode == 1, result.returncode
    assert result.stdout.strip() == 'Cloud fetch failed; existing configuration retained.', result.stdout
    assert s.snapshot(target) == before
    print('Frozen binary: dependencies load, offline error is redacted, config unchanged.')
    connection_before = s.snapshot(config)
    configured = subprocess.run([str(Path(sys.argv[1]).resolve()), 'config', '--url', 'https://127.0.0.1:1'], env=dict(os.environ,CODEX_SYNC_CONFIG=str(config)), capture_output=True, text=True, timeout=25)
    assert configured.returncode == 1
    assert configured.stdout.strip() == 'Cloud fetch failed; existing configuration retained.', configured.stdout
    assert s.snapshot(config) == connection_before
    assert s.snapshot(target) == before
    print('Frozen config command: failed verification preserves connection and Codex files.')
