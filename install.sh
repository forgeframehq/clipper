#!/usr/bin/env bash
# One-shot installer for Ubuntu. Run from inside the repo:  bash install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "==> System packages"
apt-get update -qq
apt-get install -y -qq ffmpeg python3-venv python3-pip fonts-dejavu-core unzip curl git

echo "==> Deno (yt-dlp needs a JavaScript runtime for YouTube)"
if ! command -v deno >/dev/null; then
  curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null
  ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno
fi

echo "==> Python venv + packages"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "==> Whisper model (one-time download)"
MODEL=$(grep -E '^\s*model:' config.example.yaml | head -1 | awk '{print $2}')
./venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('${MODEL:-small}', device='cpu', compute_type='int8')" \
  || echo "   (model download failed - it will retry on first run)"

[ -f config.yaml ] || cp config.example.yaml config.yaml

echo "==> Scheduled run every 30 min (systemd timer)"
sed "s#__DIR__#$DIR#g" systemd/clipper.service > /etc/systemd/system/clipper.service
cp systemd/clipper.timer /etc/systemd/system/clipper.timer
systemctl daemon-reload
echo "   Timer installed but NOT started. Start it when ready:  systemctl enable --now clipper.timer"

echo
echo "Done. Next:"
echo "  1. nano config.yaml          (streamers + filters)"
echo "  2. ./venv/bin/python run.py check"
echo "  3. ./venv/bin/python run.py process <VOD URL> --name 'Streamer'   (test one)"
echo "  4. systemctl enable --now clipper.timer"
