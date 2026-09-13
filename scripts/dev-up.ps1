<#
.SYNOPSIS
    Load .env.local and start every MedAuth service plus the web app.

.DESCRIPTION
    No service in this repository reads a .env file -- values come from the
    process environment only, which is the right call for a system handling PHI
    (see docs/operations/local-development.md, "Configuration") and which leaves
    a gap on Windows, where there is no `source .env.local`. This script is that
    missing step, and nothing more: it exports what .env.local declares and
    starts the processes that need it.

    It starts the six application services and the Vite dev server. The backing
    services come from `docker compose`, which it will bring up if they are not
    already running.

    Two module paths are in play and both are correct. track-a-clinical,
    track-b-rag and prior-auth have renamed their packages, so their app lives at
    `<package>.main:app`; audio-ingestion, fhir-integration and nudge-service
    still declare `packages = ["src"]` and theirs is at `src.main:app`. That
    second form only resolves to the right service because uvicorn is launched
    from that service's own directory -- all three install one shared top-level
    `src` into the workspace venv (ADR-0002), and from anywhere else the import
    lands in audio-ingestion.

.PARAMETER Background
    Run each service as a background job writing to a log file under
    .dev-logs/, instead of opening a window per service.

.PARAMETER SkipWeb
    Do not start the Vite dev server.

.EXAMPLE
    ./scripts/dev-up.ps1
#>
[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$SkipWeb
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }

# --- environment ----------------------------------------------------------
$envFile = Join-Path $repoRoot '.env.local'
if (-not (Test-Path $envFile)) {
    throw ".env.local not found. Copy .env.example to .env.local and fill it in."
}

$loaded = 0
$blank = New-Object System.Collections.Generic.List[string]
foreach ($line in Get-Content $envFile) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }
    $name = $trimmed.Substring(0, $split).Trim()
    $value = $trimmed.Substring($split + 1).Trim()
    # An empty variable is treated as unset by every Settings class in this
    # repo, so it is left unexported rather than exported as "". Exporting it
    # would turn "nobody configured this" into "somebody configured it to the
    # empty string", which is a different thing and defeats the defaults.
    if ($value -eq '') { $blank.Add($name); continue }
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    $loaded++
}
Write-Step "Loaded $loaded variables from .env.local ($($blank.Count) left empty)"

foreach ($required in @('JWT_SIGNING_KEY', 'DATABASE_URL', 'SMART_WEB_RETURN_URL')) {
    if (-not [Environment]::GetEnvironmentVariable($required, 'Process')) {
        throw "$required is empty in .env.local. Services that need it refuse to start, by design."
    }
}

# --- backing services -----------------------------------------------------
Write-Step 'Checking the docker compose stack'
$running = @(docker compose ps --services --status running 2>$null)
$wanted = @('postgres', 'redis', 'qdrant', 'hapi-fhir', 'crd')
$missing = $wanted | Where-Object { $running -notcontains $_ }
if ($missing.Count -gt 0) {
    Write-Warn "Starting: $($missing -join ', ')"
    docker compose up -d
    # HAPI FHIR is deliberately excluded from --wait: it ships no healthcheck
    # (the image is distroless, so every probe form fails), so --wait would
    # return immediately for it and block on nothing useful. Readiness is
    # checked from outside, against the published port.
    docker compose up -d --wait postgres redis qdrant | Out-Null
} else {
    Write-Host '    all five already running'
}

# --- application services -------------------------------------------------
$services = @(
    @{ Name = 'audio-ingestion';  Dir = 'services/audio-ingestion';  Module = 'src.main:app';              Port = 8001 },
    @{ Name = 'track-b-rag';      Dir = 'services/track-b-rag';      Module = 'track_b_rag.main:app';      Port = 8002 },
    @{ Name = 'track-a-clinical'; Dir = 'services/track-a-clinical'; Module = 'track_a_clinical.main:app'; Port = 8003 },
    @{ Name = 'fhir-integration'; Dir = 'services/fhir-integration'; Module = 'src.main:app';              Port = 8004 },
    @{ Name = 'nudge-service';    Dir = 'services/nudge-service';    Module = 'src.main:app';              Port = 8005 },
    @{ Name = 'prior-auth';       Dir = 'services/prior-auth';       Module = 'prior_auth.main:app';       Port = 8007 }
)

$logDir = Join-Path $repoRoot '.dev-logs'
if ($Background) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

# `*> file` in Windows PowerShell writes UTF-16LE, which turns every log into
# something `tail`, `grep` and the editor all render as spaced-out gibberish.
# Piping through Out-File with an explicit encoding is what keeps them readable.
$redirect = "2>&1 | Out-File -Encoding utf8 -FilePath"

Write-Step 'Starting application services'
foreach ($service in $services) {
    $dir = Join-Path $repoRoot $service.Dir
    $command = "uv run uvicorn $($service.Module) --reload --port $($service.Port)"
    if ($Background) {
        $log = Join-Path $logDir "$($service.Name).log"
        Start-Process -FilePath 'powershell' -WorkingDirectory $dir -WindowStyle Hidden `
            -ArgumentList '-NoProfile', '-Command', "$command $redirect '$log'"
        Write-Host "    $($service.Name.PadRight(17)) :$($service.Port)  -> $log"
    } else {
        Start-Process -FilePath 'powershell' -WorkingDirectory $dir `
            -ArgumentList '-NoExit', '-NoProfile', '-Command', "`$host.UI.RawUI.WindowTitle='$($service.Name)'; $command"
        Write-Host "    $($service.Name.PadRight(17)) :$($service.Port)  -> own window"
    }
}

if (-not $SkipWeb) {
    $webDir = Join-Path $repoRoot 'apps/web'
    if ($Background) {
        $log = Join-Path $logDir 'web.log'
        Start-Process -FilePath 'powershell' -WorkingDirectory $webDir -WindowStyle Hidden `
            -ArgumentList '-NoProfile', '-Command', "npm run dev $redirect '$log'"
        Write-Host "    $('web'.PadRight(17)) :5173  -> $log"
    } else {
        Start-Process -FilePath 'powershell' -WorkingDirectory $webDir `
            -ArgumentList '-NoExit', '-NoProfile', '-Command', "`$host.UI.RawUI.WindowTitle='web'; npm run dev"
        Write-Host "    $('web'.PadRight(17)) :5173  -> own window"
    }
}

# --- readiness ------------------------------------------------------------
Write-Step 'Waiting for health endpoints'
$deadline = (Get-Date).AddSeconds(90)
$pending = [System.Collections.Generic.List[object]]::new()
$services | ForEach-Object { $pending.Add($_) }

while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    foreach ($service in @($pending)) {
        try {
            $response = Invoke-WebRequest -Uri "http://localhost:$($service.Port)/health" `
                -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            # 503 is a real answer from these endpoints, not a failure to start:
            # a health route returns the per-dependency flags with data populated
            # and error null, which is the one documented departure from the
            # response envelope (ADR-0010). The service is up either way.
            Write-Host "    $($service.Name) ready ($($response.StatusCode))" -ForegroundColor Green
            $pending.Remove($service) | Out-Null
        } catch {
            $status = $_.Exception.Response.StatusCode.value__
            if ($status -eq 503) {
                Write-Host "    $($service.Name) up, reporting unhealthy (503)" -ForegroundColor Yellow
                $pending.Remove($service) | Out-Null
            }
        }
    }
}

foreach ($service in $pending) {
    Write-Warn "$($service.Name) did not answer on :$($service.Port) within 90s -- check its window or log"
}

Write-Host ''
Write-Step 'Up. Open http://localhost:5173'
Write-Host '    Play an encounter:  uv run python scripts/demo-encounter.py'
Write-Host '    Stop everything:    ./scripts/dev-down.ps1'
