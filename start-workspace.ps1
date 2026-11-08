$ErrorActionPreference = 'Stop'
$workspaceRoot = $PSScriptRoot
Set-Location -LiteralPath $workspaceRoot
$workspacePython = (Get-Command python -ErrorAction Stop).Source
$workspaceOrigin = (& $workspacePython -c "import os; from dotenv import dotenv_values; c=dotenv_values('.env'); print(os.getenv('ZEUS_WORKSPACE_ORIGIN') or c.get('ZEUS_WORKSPACE_ORIGIN') or 'http://127.0.0.1:8081')").TrimEnd('/')
if (-not (Test-Path -LiteralPath (Join-Path $workspaceRoot 'frontend/workspace/node_modules'))) {
    & npm.cmd install --prefix frontend/workspace --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw 'Workspace dependency installation failed.' }
}
& npm.cmd run build --prefix frontend/workspace
if ($LASTEXITCODE -ne 0) { throw 'Workspace build failed.' }
$workspaceAlive = $false
try {
    $workspaceConfig = Invoke-RestMethod -Uri "$workspaceOrigin/api/v2/config" -TimeoutSec 3
    $workspaceAlive = $workspaceConfig.version -eq 'workspace-1'
} catch { }
if (-not $workspaceAlive) {
    $workspaceLogs = Join-Path $workspaceRoot 'output'
    New-Item -ItemType Directory -Path $workspaceLogs -Force | Out-Null
    Start-Process -FilePath $workspacePython -ArgumentList '-m','zeus.workspace' -WorkingDirectory $workspaceRoot `
        -WindowStyle Hidden -RedirectStandardOutput (Join-Path $workspaceLogs 'workspace-server.stdout.log') `
        -RedirectStandardError (Join-Path $workspaceLogs 'workspace-server.stderr.log') | Out-Null
    $workspaceDeadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 300
        try {
            $workspaceConfig = Invoke-RestMethod -Uri "$workspaceOrigin/api/v2/config" -TimeoutSec 2
            $workspaceAlive = $workspaceConfig.version -eq 'workspace-1'
        } catch { }
    } until ($workspaceAlive -or (Get-Date) -gt $workspaceDeadline)
    if (-not $workspaceAlive) { throw 'Workspace did not start. See output/workspace-server.stderr.log.' }
}
Start-Process $workspaceOrigin
