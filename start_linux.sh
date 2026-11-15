#!/usr/bin/env bash
cd "$(dirname "$0")"
PY=python3; command -v $PY >/dev/null || PY=python
echo "Updating download engine (yt-dlp, gallery-dl, ffmpeg, Deno)..."
$PY -m pip install -U --quiet --disable-pip-version-check "yt-dlp[default,curl-cffi]" gallery-dl imageio-ffmpeg deno pywebview 2>/dev/null \
  || $PY -m pip install -U --quiet --user "yt-dlp[default,curl-cffi]" gallery-dl imageio-ffmpeg deno pywebview 2>/dev/null \
  || $PY -m pip install -U --quiet --break-system-packages "yt-dlp[default,curl-cffi]" gallery-dl imageio-ffmpeg deno pywebview
exec $PY app/mo_downloader.py "$@"
