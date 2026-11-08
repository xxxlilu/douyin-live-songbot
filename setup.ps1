# ============================================================
#  抖音直播间点歌机 · 首次安装脚本
#
#  用法：
#    双击 Setup.bat            （推荐）
#    或  powershell -ExecutionPolicy Bypass -File setup.ps1
#
#  可选参数：
#    -RoomUrl https://live.douyin.com/123456789   直接指定直播间
#    -Proxy   http://127.0.0.1:7890               下载慢时给个代理
#    -Mirror  https://pypi.tuna.tsinghua.edu.cn/simple
#    -SkipDeps                                    跳过虚拟环境和依赖（只改直播间配置）
# ============================================================
param(
    [string]$RoomUrl = "",
    [string]$Proxy = "",
    [string]$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$root    = $PSScriptRoot
$songbot = Join-Path $root "songbot"
$overlay = Join-Path $root "overlay"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Fail($text) {
    Write-Host ""
    Write-Host "✗ $text" -ForegroundColor Red
    Write-Host ""
    exit 1
}

Say ""
Say "============================================================" -ForegroundColor Cyan
Say "  抖音直播间点歌机 · 首次安装" -ForegroundColor Cyan
Say "============================================================" -ForegroundColor Cyan
Say ""

if ($Proxy) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    Say "使用代理：$Proxy" DarkGray
}

# ---------------------------------------------------------------- 1. Python
Say "== 1/6 找 Python ==" -ForegroundColor Yellow
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
        Say "    $c  ($v)" Green
        break
    }
}
if (-not $base) {
    Say ""
    Say "没找到 Python 3。装一个再回来：" -ForegroundColor Red
    Say "  方式一：微软商店搜 Python 3.12"
    Say "  方式二：https://www.python.org/downloads/windows/"
    Say "  方式三（推荐）：在终端执行  winget install Python.Python.3.12"
    Fail "缺少 Python 3"
}

# ---------------------------------------------------------------- 2. venv
Say ""
Say "== 2/6 建虚拟环境 ==" -ForegroundColor Yellow
$venv = Join-Path $songbot ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if ($SkipDeps) {
    Say "    -SkipDeps：跳过" DarkGray
} elseif (-not (Test-Path $py)) {
    & $base -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { Fail "虚拟环境创建失败：$venv" }
    Say "    已创建 $venv" Green
} else {
    Say "    已存在，跳过" DarkGray
}

# pip 缓存放项目里，避免 %LOCALAPPDATA%\pip\cache 的权限问题
$cache = Join-Path $root ".cache"
New-Item -ItemType Directory -Force -Path (Join-Path $cache "pip") | Out-Null
$env:PIP_CACHE_DIR = Join-Path $cache "pip"

# 有些机器 %TEMP% 可写性异常（杀软/权限），pip 会卡在临时目录上；能写就用项目内的
$tempDir = Join-Path $cache "tmp"
try {
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    $probe = Join-Path $tempDir ("probe-" + [guid]::NewGuid().ToString("N"))
    [System.IO.File]::WriteAllText($probe, "x")
    Remove-Item $probe -Force
    $env:TEMP = $tempDir
    $env:TMP = $tempDir
} catch {
    Say "    （系统临时目录可用，使用默认）" DarkGray
}

# ---------------------------------------------------------------- 3. 依赖
Say ""
Say "== 3/6 装 Python 依赖 ==" -ForegroundColor Yellow
if ($SkipDeps) {
    Say "    -SkipDeps：跳过" DarkGray
} else {
    & $py -m pip install --disable-pip-version-check --retries 2 --timeout 20 -U pip -i $Mirror
    & $py -m pip install --disable-pip-version-check --retries 2 --timeout 20 -r (Join-Path $songbot "requirements.txt") -i $Mirror
    if ($LASTEXITCODE -ne 0) { Fail "主程序依赖装失败（换 -Mirror 或加 -Proxy 再试）" }
    & $py -m pip install --disable-pip-version-check --retries 2 --timeout 20 "websocket-client" "betterproto" "protobuf" -i $Mirror
    if ($LASTEXITCODE -ne 0) { Fail "弹幕库依赖装失败（换 -Mirror 或加 -Proxy 再试）" }
    Say "    依赖 OK" Green
}

# ---------------------------------------------------------------- 4. 弹幕库
Say ""
Say "== 4/6 拉取抖音弹幕库 ==" -ForegroundColor Yellow
$vendorDir = Join-Path $songbot "vendor"
$vendor = Join-Path $vendorDir "DouyinLiveWebFetcher"
if (Test-Path (Join-Path $vendor "liveMan.py")) {
    Say "    已存在，跳过" DarkGray
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Say ""
        Say "需要 git 来拉取弹幕库：https://git-scm.com/download/win" -ForegroundColor Red
        Fail "缺少 git"
    }
    New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null
    & git clone --depth 1 https://github.com/saermart/DouyinLiveWebFetcher $vendor
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $vendor "liveMan.py"))) {
        Fail "弹幕库拉取失败（网络问题？可以加 -Proxy http://127.0.0.1:7890）"
    }
    Say "    已拉取到 songbot\vendor\DouyinLiveWebFetcher" Green
}

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Say "    ⚠ 没装 Node.js：弹幕签名需要它（或 pip install py-mini-racer）" -ForegroundColor Yellow
    Say "      https://nodejs.org/zh-cn  （装 LTS 就行）" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 5. 连接器
Say ""
Say "== 5/6 安装网易云连接器 ==" -ForegroundColor Yellow
$connector = Join-Path $songbot "tools\awoo-connector\extracted\Awoo.Connector.Netease.exe"
if (Test-Path $connector) {
    Say "    已存在，跳过" DarkGray
} else {
    Push-Location $songbot
    try {
        & $py (Join-Path $songbot "tools\install_connector.py")
        if ($LASTEXITCODE -ne 0) { Fail "连接器下载失败（网络问题？可以加 -Proxy http://127.0.0.1:7890）" }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path $connector)) { Fail "连接器没装上：$connector" }
    Say "    已安装" Green
}

# ---------------------------------------------------------------- 6. 直播间
Say ""
Say "== 6/6 设置直播间 ==" -ForegroundColor Yellow
$configPath = Join-Path $songbot "config.ini"
if (-not (Test-Path $configPath)) { Fail "找不到 songbot\config.ini" }

$current = ""
$text = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8)
if ($text -match "(?m)^\s*room_url\s*=\s*(.+?)\s*$") { $current = $Matches[1] }

if (-not $RoomUrl) {
    Say "    当前：$current" DarkGray
    Say "    在抖音网页版/直播伴侣里复制你的直播间地址，形如："
    Say "        https://live.douyin.com/123456789" DarkGray
    Say ""
    $RoomUrl = Read-Host "    粘贴直播间链接（直接回车=不改）"
}

if ($RoomUrl) {
    $RoomUrl = $RoomUrl.Trim()
    if ($RoomUrl -match "^(\d+)$") {
        $RoomUrl = "https://live.douyin.com/$($Matches[1])"
    } elseif ($RoomUrl -match "^live\.douyin\.com/(\d+)") {
        $RoomUrl = "https://live.douyin.com/$($Matches[1])"
    } elseif ($RoomUrl -notmatch "^https?://") {
        $RoomUrl = "https://$RoomUrl"
    }
    if ($RoomUrl -notmatch "^https://live\.douyin\.com/\d+") {
        Fail "链接看起来不对：$RoomUrl（应该是 https://live.douyin.com/一串数字）"
    }
    if ($text -match "(?m)^\s*room_url\s*=") {
        $text = [regex]::Replace($text, "(?m)^\s*room_url\s*=.*$", "room_url = $RoomUrl")
    } else {
        $text = $text -replace "(?m)^\[general\]\s*$", "[general]`r`nroom_url = $RoomUrl"
    }
    [System.IO.File]::WriteAllText($configPath, $text, (New-Object System.Text.UTF8Encoding($false)))
    Say "    已保存：$RoomUrl" Green
}

# ---------------------------------------------------------------- 自检
Say ""
Say "== 自检 ==" -ForegroundColor Yellow
Push-Location $songbot
try {
    & $py -c "import sys, requests, websocket, betterproto; print('  python', sys.version.split()[0]); print('  requests / websocket-client / betterproto  OK')"
    & $py -m songbot doctor 2>&1 | Select-String "触发词|控制|收件箱|目录"
} finally {
    Pop-Location
}

Say ""
Say "============================================================" -ForegroundColor Cyan
Say "  装好了" -ForegroundColor Green
Say "============================================================" -ForegroundColor Cyan
Say "  下一步："
Say "    1. 打开网易云音乐，随便放一首歌"
Say "    2. 双击项目根目录的 Start.bat（会弹 UAC）"
Say "    3. 浏览器会自动打开叠加层和控制面板"
Say ""
Say "  手机控制：启动窗口里会打印「手机访问」地址（带口令）"
Say "============================================================" -ForegroundColor Cyan
Say ""
