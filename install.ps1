$ErrorActionPreference = 'Stop'
$shareRoot = $PSScriptRoot
$envPath = Join-Path $shareRoot '.venv'
$envPython = Join-Path $envPath 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $envPython)) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { throw 'Install Python 3.11 or 3.12 from python.org, including the Python launcher, then retry.' }
    $installedVersions = (& py -0p 2>$null) -join "`n"
    $pythonVersion = if ($installedVersions -match '3\.11') { '-3.11' } elseif ($installedVersions -match '3\.12') { '-3.12' } else { throw 'Python 3.11/3.12 is required.' }
    & py $pythonVersion -m venv $envPath
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11/3.12 is required.' }
}
& $envPython -m pip install -r (Join-Path $shareRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the network and retry; do not start collection yet.' }
& $envPython -c 'import openpyxl, requests, rapidocr_onnxruntime, comtypes; print("Dependencies ready")'
if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed.' }
Write-Host 'Installation complete. Double-click the launcher to open the local workspace.'
