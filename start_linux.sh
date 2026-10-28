#!/usr/bin/env bash
cd "$(dirname "$0")"
PY=python3; command -v $PY >/dev/null || PY=python
echo "Updating download engine (yt-dlp, ffmpeg, Deno)..."
$PY -m pip install -U --quiet --disable-pip-version-check "yt-dlp[default]" imageio-ffmpeg deno pywebview 2>/dev/null \
  || $PY -m pip install -U --quiet --user "yt-dlp[default]" imageio-ffmpeg deno pywebview 2>/dev/null \
  || $PY -m pip install -U --quiet --break-system-packages "yt-dlp[default]" imageio-ffmpeg deno pywebview
exec $PY app/mo_downloader.py "$@"
