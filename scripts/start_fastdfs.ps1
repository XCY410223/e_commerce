$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker engine is not running. Start Docker Desktop first."
}

Write-Host "Pulling FastDFS, Nginx and uploader images..." -ForegroundColor Cyan
docker compose pull
if ($LASTEXITCODE -ne 0) {
    throw "Image pull failed. Check the network or Docker Desktop registry settings."
}

Write-Host "Starting FastDFS services..." -ForegroundColor Cyan
docker compose up -d
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed."
}

$healthUrl = "http://127.0.0.1:8888/healthz"
$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
    }
    Start-Sleep -Seconds 2
}

if (-not $ready) {
    docker compose ps
    throw "FastDFS gateway did not become ready. Run docker compose logs for details."
}

Write-Host "FastDFS gateway is ready: $healthUrl" -ForegroundColor Green
docker compose ps
& (Join-Path $projectRoot ".venv\Scripts\python.exe") (Join-Path $projectRoot "scripts\check_fastdfs.py")
