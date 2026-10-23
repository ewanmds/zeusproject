$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$botExecutable = Join-Path $PSScriptRoot 'bin\bot.exe'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'bin\bot.direct.exe')) {
    $botExecutable = Join-Path $PSScriptRoot 'bin\bot.direct.exe'
}
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'bin\bot.fixed.exe')) {
    $botExecutable = Join-Path $PSScriptRoot 'bin\bot.fixed.exe'
}
if ((Get-Content -LiteralPath config.yaml -Raw) -notmatch '(?m)^\s+live_mode:\s*false\s*(#.*)?$') {
    throw 'Le lanceur exige live_mode: false dans config.yaml.'
}
New-Item -ItemType Directory -Force logs | Out-Null
$logPath = Join-Path 'logs' ('paper-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.jsonl')
& $botExecutable -config config.yaml 2>&1 | Tee-Object -FilePath $logPath
if ($LASTEXITCODE -ne 0) { throw "Bot termine avec le code $LASTEXITCODE" }
