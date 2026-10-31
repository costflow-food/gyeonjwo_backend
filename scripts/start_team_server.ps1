param(
    [ValidateRange(1024,65535)][int]$Port=8000,
    [string]$FrontendOrigin='http://localhost:3000',
    [switch]$Publish,
    [switch]$Offline
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
$oldLegacyWorker=$env:WORKER_ENABLED
$oldConnectedWorker=$env:GOOGLE_MAILBOX_WORKER_ENABLED
Push-Location -LiteralPath $root
try {
    if ($Offline -and $Publish) { throw '-Offline cannot publish an external endpoint.' }
    if ($Offline) {
        $env:WORKER_ENABLED='false'
        $env:GOOGLE_MAILBOX_WORKER_ENABLED='false'
    }
    if (-not (Test-Path -LiteralPath $python)) { throw 'Backend .venv is required.' }
    $preflightArgs=@('-m','scripts.check_integration','--local-only')
    if (-not $Offline) { $preflightArgs+='--db' }
    & $python @preflightArgs
    if ($LASTEXITCODE -ne 0) { throw 'Configuration/database preflight failed. No Docker service was changed.' }
    & docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine unavailable. Start Docker Desktop and use an authorized local terminal.' }
    $oldPort=$env:BACKEND_PORT
    $oldRevision=$env:BUILD_REVISION
    try {
        $env:BACKEND_PORT=[string]$Port
        $revision=& git -c "safe.directory=$($root.Replace('\','/'))" rev-parse --short HEAD
        if ($LASTEXITCODE -ne 0) { throw 'Unable to read build revision.' }
        $dirty=& git -c "safe.directory=$($root.Replace('\','/'))" status --porcelain
        $env:BUILD_REVISION=if ($dirty) { "$revision-dirty" } else { $revision }
        $composeArgs=@('compose','--project-directory',$root,'--env-file',(Join-Path $root '.env'),'-f',(Join-Path $root 'compose.yaml'))
        if ($Publish) { $composeArgs+=@('-f',(Join-Path $root 'compose.https.yaml'),'--profile','https') }
        & docker @composeArgs up -d --build --force-recreate api
        if ($LASTEXITCODE -ne 0) { throw 'Docker build/start failed.' }
        $healthy=$false
        for ($attempt=0;$attempt -lt 20;$attempt++) {
            try {
                $probe=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
                if ($probe.build_revision -eq $env:BUILD_REVISION) { $healthy=$true;break }
            } catch { }
            Start-Sleep -Seconds 1
        }
        if (-not $healthy) { throw 'The listening server does not match the new build. Check Docker ports and old processes.' }
        $checkArgs=@('-m','scripts.check_integration','--base-url',"http://127.0.0.1:$Port",'--origin',$FrontendOrigin)
        if ($Offline) { $checkArgs+='--skip-readiness' }
        & $python @checkArgs
        if ($LASTEXITCODE -ne 0) { throw 'Server readiness/CORS check failed. Tunnel was not started.' }
        if ($Publish) {
            & docker @composeArgs up -d --force-recreate tunnel
            if ($LASTEXITCODE -ne 0) { throw 'Tunnel failed to start.' }
            Write-Host 'Development HTTPS endpoint published. Copy its trycloudflare.com URL from the logs below.'
            & docker @composeArgs logs --tail 40 tunnel
        } else {
            Write-Host "Local API ready: http://127.0.0.1:$Port/docs"
            if ($Offline) { Write-Host 'Offline verification only: DB readiness was not checked, receiver workers are disabled. Account connection is not verified.' }
            Write-Host 'No external endpoint was published. -Publish explicitly starts the development HTTPS tunnel.'
        }
    } finally {
        $env:BACKEND_PORT=$oldPort; $env:BUILD_REVISION=$oldRevision
    }
} finally {
    $env:WORKER_ENABLED=$oldLegacyWorker; $env:GOOGLE_MAILBOX_WORKER_ENABLED=$oldConnectedWorker
    Pop-Location
}
