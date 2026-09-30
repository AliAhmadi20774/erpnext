$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'ساخت محیط مجازی ناموفق بود.' }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'نصب وابستگی‌ها ناموفق بود.' }

& $python manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'آماده‌سازی پایگاه‌داده ناموفق بود.' }
& $python manage.py seed_demo
if ($LASTEXITCODE -ne 0) { throw 'ساخت داده‌های نمونه ناموفق بود.' }

Write-Host 'Demo is ready at http://127.0.0.1:8000/'
& $python manage.py runserver 127.0.0.1:8000
