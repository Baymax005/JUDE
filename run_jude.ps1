param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$env:JUDE_ROOT = $RootDir
$env:PIP_CACHE_DIR = Join-Path $RootDir "cache\pip"
$env:HF_HOME = Join-Path $RootDir "cache\huggingface"
$env:TORCH_HOME = Join-Path $RootDir "cache\torch"
$env:XDG_CACHE_HOME = Join-Path $RootDir "cache\xdg"
$env:TEMP = Join-Path $RootDir "cache\tmp"
$env:TMP = Join-Path $RootDir "cache\tmp"
$env:OLLAMA_MODELS = Join-Path $RootDir "models\ollama"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = $RootDir

$pythonExe = Join-Path $RootDir "venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Virtual environment not found at $pythonExe"
}

if (-not $ScriptArgs -or $ScriptArgs.Count -eq 0) {
    & $pythonExe -m app.main
} else {
    & $pythonExe @ScriptArgs
}
exit $LASTEXITCODE
