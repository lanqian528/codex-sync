#!/usr/bin/env bash
# Run from a downloaded release, or: bash <(curl -fsSL .../scripts/install.sh)
set -euo pipefail
umask 077
repo="${CODEX_SYNC_REPO:-lanqian528/codex-sync}"
version="${CODEX_SYNC_VERSION:-latest}"
[[ "$repo" =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ ]] || exit 1
[[ "$version" =~ ^(latest|v[a-zA-Z0-9_.-]+)$ ]] || exit 1
[[ "$(uname -s)" == Linux ]] || { echo 'This installer requires Linux.'; exit 1; }
case "$(uname -m)" in x86_64) arch=x64;; aarch64|arm64) arch=arm64;; *) echo 'Unsupported CPU'; exit 1;; esac
for cmd in curl unzip sha256sum systemctl; do command -v "$cmd" >/dev/null || { echo "Missing: $cmd"; exit 1; }; done
dest="$HOME/.local/share/codex-sync"
config="$HOME/.config/codex-sync"
mkdir -p "$dest" "$config"
[[ ! -L "$dest" && ! -L "$config" ]] || { echo 'Symlink directories unsupported'; exit 1; }
chmod 700 "$dest" "$config"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
base="https://github.com/$repo/releases/latest/download"
[[ "$version" == latest ]] || base="https://github.com/$repo/releases/download/$version"
asset="codex-sync-linux-$arch.zip"
curl --proto '=https' --tlsv1.2 -fsSL "$base/$asset" -o "$tmp/$asset"
curl --proto '=https' --tlsv1.2 -fsSL "$base/$asset.sha256" -o "$tmp/$asset.sha256"
(cd "$tmp" && sha256sum -c "$asset.sha256")
unzip -q "$tmp/$asset" -d "$tmp/extracted"
binary="$tmp/extracted/codex-sync-linux-$arch/codex-sync"
chmod 700 "$binary"
"$binary" --help >/dev/null
systemctl --user stop codex-sync.service 2>/dev/null || true
install -m 700 "$binary" "$dest/codex-sync.new"
mv -f "$dest/codex-sync.new" "$dest/codex-sync"
if [[ ! -e "$config/connection.json" ]]; then
  echo 'First confirm Pro works. Exit Codex before the first sync.'
  read -r -p 'Cloud HTTPS URL (Vercel or Cloudflare): ' url </dev/tty
  read -r -s -p 'Cloud ACCESS_KEY (32+ characters): ' access_key </dev/tty; echo
  read -r -p "Codex directory [${CODEX_HOME:-$HOME/.codex}]: " codex_dir </dev/tty
  codex_dir="${codex_dir:-${CODEX_HOME:-$HOME/.codex}}"
  command -v python3 >/dev/null || { echo 'Python 3 required only for initial secure JSON setup.'; exit 1; }
  printf '%s\0%s\0%s' "$url" "$access_key" "$codex_dir" | python3 -c 'import json,sys,os; data=sys.stdin.buffer.read().decode().split("\0"); p=sys.argv[1]; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); f=os.fdopen(fd,"w"); json.dump(dict(zip(["url","access_key","codex_home"],data)),f); f.close()' "$config/connection.json"
  unset access_key
  [[ -d "$codex_dir" && ! -L "$codex_dir" && ! -L "$codex_dir/config.toml" ]] || { echo 'Initialize Pro in a normal directory first'; exit 1; }
  chmod 700 "$codex_dir"
  [[ ! -e "$codex_dir/config.toml" ]] || chmod 600 "$codex_dir/config.toml"
fi
mkdir -p "$HOME/.config/systemd/user"
# systemd interprets %, backslashes and quotes: reject uncommon HOME paths safely.
[[ "$HOME" != *'%'* && "$HOME" != *'"'* && "$HOME" != *'\'* && "$HOME" != *$'\n'* ]] || { echo 'Unsupported HOME path for systemd unit'; exit 1; }
cat > "$HOME/.config/systemd/user/codex-sync.service" <<EOF
[Unit]
Description=Codex provider sync (per-user)
After=network-online.target
[Service]
ExecStart="$dest/codex-sync" watch
Environment="CODEX_SYNC_CONFIG=$config/connection.json"
Restart=on-failure
RestartSec=60
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now codex-sync.service
echo 'Installed. Check: journalctl --user -u codex-sync -n 20'
echo 'For start at boot before login: sudo loginctl enable-linger "$USER"'
