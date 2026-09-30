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

& $python manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Failed to prepare the database.' }
& $python manage.py seed_demo
if ($LASTEXITCODE -ne 0) { throw 'Failed to create demo data.' }

Write-Host 'Demo is ready at http://127.0.0.1:8000/'
& $python manage.py runserver 127.0.0.1:8000
