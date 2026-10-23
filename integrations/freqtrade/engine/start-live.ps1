param([switch]$CheckOnly, [string]$ConfigPath = 'config.live.yaml')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$botExecutable = Join-Path $PSScriptRoot 'bin\bot.exe'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'bin\bot.updated.exe')) {
    $botExecutable = Join-Path $PSScriptRoot 'bin\bot.updated.exe'
}
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'bin\bot.direct.exe')) {
    $botExecutable = Join-Path $PSScriptRoot 'bin\bot.direct.exe'
}
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'bin\bot.fixed.exe')) {
    $botExecutable = Join-Path $PSScriptRoot 'bin\bot.fixed.exe'
}
$previousKey = $env:PRIVATE_KEY
$previousAddress = $env:WALLET_ADDRESS
$keyPointer = [IntPtr]::Zero
try {
    $env:WALLET_ADDRESS = (Read-Host 'Adresse du wallet principal (0x...)').Trim()
    if ($env:WALLET_ADDRESS -notmatch '^0x[0-9a-fA-F]{40}$') { throw 'Adresse wallet invalide.' }
    $secureKey = Read-Host 'Cle privee de l API wallet autorise (saisie masquee)' -AsSecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $env:PRIVATE_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer).Trim()
    if ($env:PRIVATE_KEY -notmatch '^(0x)?[0-9a-fA-F]{64}$') { throw 'Format de cle API invalide.' }
    & $botExecutable -config $ConfigPath -check-account
    if ($LASTEXITCODE -ne 0) { throw 'Verification du compte echouee. Aucun trading lance.' }
    if ($CheckOnly) { return }
    Write-Host 'Configuration reelle :' -ForegroundColor Yellow
    Get-Content -LiteralPath $ConfigPath | Select-String '^data_source:|assets:|quote_notional:|ladder_levels:|max_position_notional:|max_spread_exposure:|max_drawdown_pct:|leverage:'
    Write-Host 'Envoi direct a Hyperliquid, sans frais builder. Les frais de trading Hyperliquid restent applicables.'
    if ((Read-Host 'Tape LIVE pour lancer les ordres reels avec ces parametres') -cne 'LIVE') { Write-Host 'Annule.'; return }
    & $botExecutable -config $ConfigPath
    if ($LASTEXITCODE -ne 0) { throw "Bot termine avec le code $LASTEXITCODE" }
} finally {
    if ($keyPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
    if ($secureKey) { $secureKey.Dispose() }
    $env:PRIVATE_KEY = $previousKey
    $env:WALLET_ADDRESS = $previousAddress
}
