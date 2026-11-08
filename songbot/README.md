# 抖音直播弹幕点歌 → 网易云「下一首播放」

主播在直播伴侣开播，观众发 **「点歌 起风了」**，程序自动在网易云里搜到原唱，
把歌插到**当前播放的下一首**——不弹窗口、不抢焦点、不碰鼠标。

> **现状（2026-11-08 真机验证，已可试用）**
>
> ✅ 网易云段：`连接：True` → 搜索《起风了》→ `插入：verified`，
> 「下一首」确实变成了目标曲；客户端 3.1.42。
> ✅ 抖音段：WebSocket 连通，能收到真实弹幕（含 `点歌 起风了`）。
> 两段都已单独验证，合起来跑就是 `.\run.ps1 run`。

## 日常启动（推荐）

双击 **`启动点歌机.bat`**。

它会：

1. 发现没有管理员权限 → 自己弹 UAC 提权重开（连接器需要管理员才能挂桥）；
2. 检查网易云在不在，不在就帮你拉起来；
3. 从 `config.ini` 读直播间地址，占位地址会直接拦住；
4. 启动监听，按 `Ctrl+C` 停止，退出后窗口不关，能看最后几行报错。

给这个 bat 建个桌面快捷方式就行，以后开播双击一下。

> 注意：bat 文件是 **GBK + CRLF** 编码的，别用会改编码的编辑器保存，
> 否则 cmd 会满屏报 “'xxx' is not recognized”。要改就改 `.ps1` 或告诉我。

## 它是怎么控住网易云的

**不是**界面自动化，也**不是**老式的 orpheus 窗口消息（那条通道在网易云 3.1.41+ 已废弃）。

现在复用的是 MIT 项目 [嗷呜点歌机](https://github.com/Enkianssus/BiliNCM-TS) 的
网易云连接器（`Awoo.Connector.Netease.exe`，版本 `3.1.41.205529.2`，
`playerVersionPolicy = 3.1.*`，对 3.1.42 同样有效）：

| 动作 | 做法 |
| --- | --- |
| 搜歌 | 连接器的 `search`（内部走 `music.163.com` 多端点容错） |
| 下一首播放 | 连接器往客户端进程内注入 CEF 桥，直接调用它的 Redux `ADD_NEXT` |
| 确认生效 | 连接器订阅客户端 Redux 事件，回报 `verified` 并启动「错歌兜底」 |
| 通信 | 以子进程方式启动，stdin/stdout 走 NDJSON：`ping` / `probe` / `search` / `execute` / `shutdown` |

好处：不抢焦点、不看界面、能自证成功。代价：连接器是别人维护的，
版本敏感，所以下载时做了 sha256 校验（`tools/install_connector.py`）。

> 老的 `orpheus://` 方案（`[netease] mode = ipc`）保留着，只用于排查历史问题；
> 3.1.41+ 上它会把消息投递成功但客户端不执行。

## 工作方式

```
抖音直播间 ──(DouyinLiveWebFetcher)──> 本地 Python ──(Orpheus IPC)──> 网易云客户端
                                            │
                                            ├─ 认「点歌 歌名 歌手」
                                            ├─ 防刷（同一人冷却、同歌去重、排队上限）
                                            └─ 日志 logs/songbot.log
```

## 一、装依赖

`.venv` 已经建好并装完依赖了（requests / websocket-client / betterproto），直接跳到第二步。

换机器或删了 `.venv` 时，重新跑：

```powershell
.\setup.ps1
```

它会建项目独立的 `.venv`，不污染你的 conda base。

### 网络要走代理时

抖音和 PyPI 直连不通的话，先在同一个窗口设代理，再跑命令：

```powershell
$env:HTTP_PROXY  = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
.\run.ps1 doctor
```

`run.ps1` 会继承这两个变量，弹幕子进程也能走。

## 二、接抖音弹幕（唯一还没通的一步）

```powershell
git clone https://github.com/saermart/DouyinLiveWebFetcher vendor/DouyinLiveWebFetcher
.\setup.ps1
```

拉不到就用浏览器下 zip 解压到 `vendor\DouyinLiveWebFetcher\`。

> 这个库的入口是 `liveMan.py`，不是 `liveWebFetcher.py`。我们的
> [tools/douyin_ndjson.py](tools/douyin_ndjson.py) 已经按它真实的接口写好了，
> 你不需要改。

## 三、改 `config.ini`

只需要改一处：

```ini
[general]
room_url = https://live.douyin.com/你的房间号
```

弹幕命令默认就是对的：

```ini
[danmaku]
command = python tools/douyin_ndjson.py {room_url}
```

`{room_url}` 会自动替换，脚本再从里面抠出纯数字房间号（库要的是这个）。

## 四、自检

```powershell
.\run.ps1 doctor
```

第一行会告诉你用的哪个 python；正常输出长这样：

```
[songbot] python = ...\.venv\Scripts\python.exe
检查网易云（mode = ipc）…
  进程 27156  版本 3.1.42.205594
  窗口标题：心痛2009 - 欢子
  播放列表：109 首
  试搜「晴天」…
    ...
```

「没找到网易云客户端」= 客户端没开；「搜索失败」= 网络问题。

## 五、先测识别（不动网易云）

```ini
[general]
dry_run = true
```

```powershell
.\run.ps1 run
```

开播后自己发几条「点歌 起风了」「点歌 孤勇者 陈奕迅」，看控制台识别对不对。

## 六、正式跑

先打开网易云、随便放一首歌（要有「当前播放」，否则没法插「下一首」），然后：

```powershell
.\run.ps1 run
```

想单独验证「搜索 + 插队」这一跳，不动弹幕：

```powershell
.\run.ps1 test "点歌 起风了"
```

## 点歌格式

| 写法 | 结果 |
| --- | --- |
| `点歌 起风了` | 歌名 = 起风了 |
| `点歌 孤勇者 陈奕迅` | 整串去搜，命中率高 |
| `点歌 孤勇者 - 陈奕迅` | 拆成歌名 + 歌手 |
| `点歌 孤勇者｜陈奕迅` | 同上（`/`、`by` 也行） |
| `@某人 点歌 起风了` | 自动去掉 @ 前缀 |

严格模式（`[song] strict = true`）要求以「点歌」开头，且不能夹带
「能不能/帮我/谢谢」这类词；`require_artist = true` 则必须带歌手。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `.\run.ps1 run` | 正式跑 |
| `.\run.ps1 doctor` | 自检：依赖、直播间、网易云进程、搜索 |
| `.\run.ps1 test "点歌 起风了"` | 不动弹幕，测「搜索 + 插到下一首」 |
| `.\run.ps1 dump --seconds 30` | 看弹幕库原始输出（查格式用） |
| `.\run.ps1 -v run` | 详细日志 |
| `.\run.ps1 -c config.test.ini run` | 用假弹幕源跑整条链，不依赖抖音 |

## 排错：命令跑完什么都不输出

`run.ps1` 的第一行一定会打印它用的 python。如果连这一行都没有，是 PowerShell 没执行脚本；
如果只有这一行、后面没内容，是 python 的问题。

按顺序查：

```powershell
Get-Command python | Format-List Source,CommandType
python -V
python -c "print('hello')"
```

- `Source` 指向 `WindowsApps\python.exe` 或 `pythonw.exe` → 是商店别名/无控制台版本，
  换成 conda 的 python（`C:\Users\<你>\miniconda3\python.exe`），或装个官方 Python 3.12。
- `python -V` 都没有输出 → 这个 python 坏了，同上。
- `python -V` 正常但 `.\run.ps1` 没输出 → 把 `run.ps1` 那行 `[songbot] python = ...` 发我。

依赖装不上的话，先在终端确认代理能让 pip 通：

```powershell
python -m pip install --timeout 10 requests -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 注意

- **不再抢焦点**：走 IPC 不需要激活窗口，你可以一直盯着直播伴侣。
- **必须有「当前播放」**：没有正在播的歌，就没有「下一首」可插。
- **版权**：网易云没版权的歌（比如周杰伦的大部分作品）只会搜到翻唱，
  日志会提示「没找到原唱」。介意就把 `search_suffix` 填上歌手名。
- **只抓公开直播间**：粉丝团/私密/未开播的抓不到评论；直播伴侣记得开「桌面音频」。
- 这属于非官方接口，客户端大版本更新后 `playingList` / `addToNext` 可能变；
  变了就切 `[netease] mode = uia` 用界面自动化兜底。

## 目录

```
songbot/                    （项目根目录下的 songbot 文件夹）
├─ config.ini              配置
├─ run.ps1                 启动
├─ songbot/
│  ├─ __main__.py          run / test / doctor / tree / dump
│  ├─ app.py               主流程
│  ├─ parser.py            「点歌 歌名 歌手」解析
│  ├─ queue.py             防刷、去重、限流
│  ├─ danmaku/             弹幕来源适配器（command / vendor）
│  └─ music/
│     ├─ netease_ipc.py    原生 IPC 控制（默认）
│     ├─ netease.py        界面自动化（兜底，mode=uia）
│     └─ base.py           控制器接口
├─ tests/smoke_test.py     离线单测
├─ vendor/                 第三方弹幕库（你自己克隆）
└─ logs/                   运行日志
```
