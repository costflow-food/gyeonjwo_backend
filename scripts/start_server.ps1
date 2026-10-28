param(
    [ValidateSet('docker', 'local')][string]$Mode = 'docker',
    [ValidateRange(1024, 65535)][int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $backendRoot
try {
    if (-not (Test-Path -LiteralPath '.env')) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        Write-Host 'Created .env from template. Configure local credentials before external integration.'
    }
    if ($Mode -eq 'docker') {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            throw 'Docker is not installed/on PATH. Install Docker Desktop or use -Mode local.'
        }
        & docker info --format '{{.ServerVersion}}'
        if ($LASTEXITCODE -ne 0) { throw 'Docker engine is not running.' }
        $previousBackendPort = $env:BACKEND_PORT
        try {
            $env:BACKEND_PORT = [string]$Port
            & docker compose up -d --build
            if ($LASTEXITCODE -ne 0) { throw 'Docker Compose build/start failed.' }
        } finally { $env:BACKEND_PORT = $previousBackendPort }
    } else {
        $backendPython = Join-Path $backendRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $backendPython)) { throw 'Create .venv and install requirements-dev.txt first.' }
        & $backendPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1
        if ($LASTEXITCODE -ne 0) { throw 'Local server exited with an error.' }
    }
} finally { Pop-Location }
