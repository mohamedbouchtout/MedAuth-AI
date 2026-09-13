<#
.SYNOPSIS
    Stop the application services started by dev-up.ps1.

.DESCRIPTION
    Kills the uvicorn and Vite processes listening on this repository's dev
    ports, and nothing else. It matches on the port rather than on the process
    name so it cannot take down an unrelated Python or Node process that happens
    to be running on the same machine.

    The docker compose stack is deliberately left up: it holds the seeded Qdrant
    corpus, the Synthea patients in HAPI and the Postgres volume, and bringing it
    down on every stop makes a restart expensive for no benefit. Use
    `docker compose down` when you actually want that.

.PARAMETER IncludeBackingServices
    Also run `docker compose stop`.
#>
[CmdletBinding()]
param([switch]$IncludeBackingServices)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$ports = @(8001, 8002, 8003, 8004, 8005, 8007, 5173)
$stopped = 0

foreach ($port in $ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        Write-Host "Stopping $($process.ProcessName) (pid $($process.Id)) on :$port"
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $stopped++
    }
}

if ($stopped -eq 0) { Write-Host 'Nothing was listening on the dev ports.' }

if ($IncludeBackingServices) {
    Write-Host 'Stopping the docker compose stack'
    docker compose stop
}
