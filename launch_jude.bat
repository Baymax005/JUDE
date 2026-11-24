@echo off
setlocal
set "JUDE_ROOT=%~dp0"
if "%JUDE_ROOT:~-1%"=="\" set "JUDE_ROOT=%JUDE_ROOT:~0,-1%"

pushd "%JUDE_ROOT%"

set "PIP_CACHE_DIR=%JUDE_ROOT%\cache\pip"
set "HF_HOME=%JUDE_ROOT%\cache\huggingface"
set "TORCH_HOME=%JUDE_ROOT%\cache\torch"
set "XDG_CACHE_HOME=%JUDE_ROOT%\cache\xdg"
set "TEMP=%JUDE_ROOT%\cache\tmp"
set "TMP=%JUDE_ROOT%\cache\tmp"
set "OLLAMA_MODELS=%JUDE_ROOT%\models\ollama"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH=%JUDE_ROOT%"

echo Starting J.U.D.E. Floating Assistant...
start "" /D "%JUDE_ROOT%" "%JUDE_ROOT%\venv\Scripts\pythonw.exe" -m app.main
ping 127.0.0.1 -n 2 > nul
popd
endlocal
exit /b 0
