$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run start_demo.ps1 once before resetting the demo.'
}

& $python manage.py reset_demo --yes
if ($LASTEXITCODE -ne 0) { throw 'Failed to reset presentation data.' }
Write-Host 'Presentation data reset; the previous database was saved under backups.'
