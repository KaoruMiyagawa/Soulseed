$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project .venv Python was not found.' }
& $python (Join-Path $PSScriptRoot 'backend\scripts\start_development.py') @args
exit $LASTEXITCODE
