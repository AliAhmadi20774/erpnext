param([switch]$PrepareOnly)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -c "import django, jdatetime" *> $null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies.' }
}

& $python manage.py prepare_demo_database
if ($LASTEXITCODE -ne 0) { throw 'Failed to prepare the database.' }
& $python manage.py seed_demo
if ($LASTEXITCODE -ne 0) { throw 'Failed to create demo data.' }
& $python manage.py rebuild_accounting
if ($LASTEXITCODE -ne 0) { throw 'Failed to prepare accounting journals.' }

if ($PrepareOnly) {
    Write-Host 'Demo database and presentation data are ready; existing business data was kept.'
    return
}

Write-Host 'Demo is ready at http://127.0.0.1:8000/'
& $python manage.py runserver 127.0.0.1:8000
