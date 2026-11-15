#!/usr/bin/env bash
cd "$(dirname "$0")"
PY=python3; command -v $PY >/dev/null || PY=python
# first run only: install what is missing (the app keeps the engines current in the background, once a day)
if ! $PY -c "import yt_dlp, gallery_dl, curl_cffi, imageio_ffmpeg, deno" 2>/dev/null; then
  echo "Installing the download engine (yt-dlp, gallery-dl, ffmpeg, Deno)... first run only."
  $PY -m pip install -U --quiet --disable-pip-version-check "yt-dlp[default,curl-cffi]" gallery-dl imageio-ffmpeg deno pywebview 2>/dev/null \
    || $PY -m pip install -U --quiet --user "yt-dlp[default,curl-cffi]" gallery-dl imageio-ffmpeg deno pywebview 2>/dev/null \
    || $PY -m pip install -U --quiet --break-system-packages "yt-dlp[default,curl-cffi]" gallery-dl imageio-ffmpeg deno pywebview
fi
exec $PY app/mo_downloader.py "$@"
