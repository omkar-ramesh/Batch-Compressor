@echo off
setlocal
cd /d "%~dp0"
echo Building Batch Compressor.exe ...
set PY=python
if exist ".buildenv\Scripts\python.exe" set PY=.buildenv\Scripts\python.exe
%PY% make_icon.py || goto :fail
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "BatchCompressor" ^
  --icon "assets\icon.ico" ^
  --add-data "static;static" ^
  --collect-all webview ^
  --hidden-import "compressor.engine" ^
  app.py || goto :fail
echo.
echo Done: %~dp0dist\BatchCompressor.exe
exit /b 0
:fail
echo Build failed.
exit /b 1
