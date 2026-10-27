@echo off
title MO Downloader
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python is not installed.
  echo  Install it from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

rem --- Desktop + Start-menu shortcuts with the MO Downloader icon (refreshed every run)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell; foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){ $s=$ws.CreateShortcut((Join-Path $d 'MO Downloader.lnk')); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0app\mo_downloader.ico'; $s.WindowStyle=7; $s.Description='MO Downloader'; $s.Save() }" >nul 2>nul

echo Updating download engine (yt-dlp, ffmpeg, Deno)... please wait.
python -m pip install -U --quiet --disable-pip-version-check "yt-dlp[default]" imageio-ffmpeg deno

set "MO_LAUNCHER=%~f0"
if /i "%~1"=="--background" (
  start "" pythonw "%~dp0app\mo_downloader.py" --background
  exit /b 0
)
python "%~dp0app\mo_downloader.py" %*
if errorlevel 1 pause
