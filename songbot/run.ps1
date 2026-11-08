param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
try { $OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

Set-Location -LiteralPath $PSScriptRoot

# 优先用项目自己的虚拟环境，其次 PATH 里的 python
$pythonExe = $null
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonExe = $venvPython
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $pythonExe = if ($cmd.Source) { $cmd.Source } else { $cmd.FullName }
    }
}

if (-not $pythonExe) {
    Write-Host "[songbot] 找不到 python。先跑 .\setup.ps1，或把 Python 加进 PATH。" -ForegroundColor Red
    exit 1
}

Write-Host "[songbot] python = $pythonExe" -ForegroundColor DarkGray
if (-not $Rest -or $Rest.Count -eq 0) { $Rest = @("run") }

& $pythonExe -u -m songbot @Rest
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host "[songbot] 退出码 $code" -ForegroundColor Red
}
exit $code
