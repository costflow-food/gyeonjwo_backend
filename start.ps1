param([switch]$Setup, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$costflowRoot = $PSScriptRoot
$costflowPython = Join-Path $costflowRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $costflowPython)) {
    throw 'Create the project .venv and install dependencies first. See README.md.'
}
$costflowPreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $costflowRoot 'src'
    if ($Setup) {
        & $costflowPython (Join-Path $costflowRoot 'tools/setup_local.py')
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $costflowPython (Join-Path $costflowRoot 'tools/check_connection.py')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (-not $CheckOnly) {
        & $costflowPython -m uvicorn costflow.main:app --app-dir (Join-Path $costflowRoot 'src') --host 127.0.0.1 --port 8000
        exit $LASTEXITCODE
    }
} finally {
    $env:PYTHONPATH = $costflowPreviousPythonPath
}
