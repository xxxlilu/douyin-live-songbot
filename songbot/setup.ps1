# 一次性把依赖装进项目自己的 .venv，不污染你的 conda base
# 用法：.\setup.ps1
param(
    [string]$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "== 1/4 找 python ==" -ForegroundColor Cyan
# PATH 里的 python 可能是商店别名（跑起来没输出），所以按候选列表挨个试
$candidates = @()
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $candidates += $(if ($cmd.Source) { $cmd.Source } else { $cmd.FullName }) }
$candidates += (Join-Path $env:USERPROFILE "miniconda3\python.exe")
$candidates += (Join-Path $env:USERPROFILE "anaconda3\python.exe")
$pyRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
if (Test-Path $pyRoot) {
    $candidates += (Get-ChildItem $pyRoot -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
}

$base = $null
foreach ($c in $candidates) {
    if (-not $c -or -not (Test-Path $c)) { continue }
    $v = (& $c -V 2>&1) -join " "
    if ($LASTEXITCODE -eq 0 -and $v -match "Python 3") {
        $base = $c
        Write-Host "    $c  ($v)"
        break
    }
    Write-Host "    跳过（跑不起来 / 没输出）：$c" -ForegroundColor DarkGray
}
if (-not $base) {
    Write-Host "没找到一个能用的 python。装个官方 Python 3.12，或用 conda 建一个 3.12 环境。" -ForegroundColor Red
    exit 1
}

Write-Host "== 2/4 建虚拟环境 .venv ==" -ForegroundColor Cyan
$venv = Join-Path $PSScriptRoot ".venv"
if (-not (Test-Path $venv)) {
    & $base -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "虚拟环境没建成：$py" -ForegroundColor Red
    exit 1
}

Write-Host "== 3/4 装主程序依赖 ==" -ForegroundColor Cyan
& $py -m pip install --disable-pip-version-check --retries 2 --timeout 20 -U pip -i $Mirror
& $py -m pip install --disable-pip-version-check --retries 2 --timeout 20 -r requirements.txt -i $Mirror
if ($LASTEXITCODE -ne 0) { Write-Host "主程序依赖安装失败（代理/镜像问题？）" -ForegroundColor Red; exit 1 }

Write-Host "== 4/4 装弹幕库依赖 ==" -ForegroundColor Cyan
# 只装真正需要的三个；py-mini-racer / PyExecJS 有 Node 兜底，装不上也不影响
& $py -m pip install --disable-pip-version-check --retries 2 --timeout 20 `
    "requests" "websocket-client" "betterproto" -i $Mirror
if ($LASTEXITCODE -ne 0) {
    Write-Host "弹幕库依赖安装失败。检查代理（HTTP_PROXY / HTTPS_PROXY）后重跑。" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "== 校验 ==" -ForegroundColor Cyan
& $py -c "import sys, requests, websocket, betterproto; print('python', sys.version.split()[0]); print('requests / websocket-client / betterproto 都 OK')"
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) { Write-Host "   node: $($node.Source)（签名 JS 用它跑）" } else { Write-Host "   没装 node：需要 pip install py-mini-racer 才能生成签名" -ForegroundColor Yellow }

Write-Host ""
Write-Host "装完了。接着跑：.\run.ps1 doctor" -ForegroundColor Green
