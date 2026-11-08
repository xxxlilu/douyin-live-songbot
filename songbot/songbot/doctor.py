"""自检：依赖、配置、网易云能不能被我们控住。

出问题先跑这个，把输出发我，比截图有用。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .config import Config


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _node() -> str:
    import shutil

    return shutil.which("node") or ""


def print_env(cfg: Config) -> None:
    print("=" * 60)
    print("Python :", sys.version.split()[0], "|", sys.executable)
    print("项目    :", cfg.root)
    print("配置    :", cfg.path)
    print("-" * 60)
    for m in ("requests", "websocket", "betterproto", "py_mini_racer", "execjs", "uiautomation"):
        print(f"  依赖 {m:<18} {'OK' if _has(m) else '缺失'}")
    node = _node()
    print(f"  Node   {'OK  ' + node if node else '缺失（签名 JS 需要它或 py-mini-racer）'}")
    print("-" * 60)
    print("直播间 :", cfg.general.room_url)
    print("dry_run:", cfg.general.dry_run)
    print("来源   :", cfg.danmaku.source)
    if cfg.danmaku.source == "command":
        print("命令   :", cfg.danmaku.command)
    print("触发词 :", " / ".join(cfg.song.keywords))
    print("严格   :", "开" if cfg.song.strict else "关", "| 必须带歌手:", "是" if cfg.song.require_artist else "否")
    print("控制   :", cfg.netease.mode)
    print("=" * 60)


def check_vendor(cfg: Config) -> None:
    vendor = cfg.root / "vendor" / "DouyinLiveWebFetcher"
    print()
    print("弹幕库 vendor/DouyinLiveWebFetcher：", "已存在" if vendor.is_dir() else "没有")
    if not vendor.is_dir():
        print("  在项目目录执行：")
        print("    git clone https://github.com/saermart/DouyinLiveWebFetcher vendor/DouyinLiveWebFetcher")


def check_room(cfg: Config) -> None:
    """看直播间现在是不是在播——这个不开，弹幕一条都不会有。"""
    import re

    print()
    url = (cfg.general.room_url or "").strip()
    print("检查直播间状态…")
    if not url or url.endswith("000000000"):
        print("  [general] room_url 还没填")
        return
    try:
        import requests

        resp = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            },
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  打不开直播间页面：{type(exc).__name__}: {exc}")
        return

    text = resp.text
    if "直播已结束" in text:
        print("  ❌ 当前未开播（直播已结束）——这时收不到任何弹幕，开播后再跑")
    elif re.search(r'roomId\\*":\\*"?(\d+)', text):
        print("  ✅ 正在直播")
    else:
        print("  ⚠️ 页面里没找到直播状态，可能被风控，或链接不对")


def check_netease(cfg: Config) -> None:
    print()
    mode = (cfg.netease.mode or "queue").lower()
    print(f"检查网易云（mode = {mode}）…")
    if mode == "queue":
        from .music.queue_out import QueueController

        ctrl = QueueController(cfg, __import__("logging").getLogger("songbot"))
        ok = ctrl.path.parent.is_dir()
        print(f"  点歌收件箱：{ctrl.path}")
        print(f"  目录{'OK' if ok else '不存在（启动时会自动建）'}")
        print("  这个模式下 songbot 不碰网易云，排队和插播由 next5-overlay 负责。")
        return
    if mode == "uia":
        _check_netease_uia(cfg)
        return
    if mode == "awoo":
        from .music.awoo import diagnose_awoo

        print("  " + diagnose_awoo(cfg, keyword="晴天").replace("\n", "\n  "))
        return

    from .music import netease_ipc

    print("  " + netease_ipc.describe().replace("\n", "\n  "))

    print("  试搜「晴天」…")
    try:
        tracks = netease_ipc.search("晴天", limit=3)
    except Exception as exc:  # noqa: BLE001
        print(f"  搜索失败：{type(exc).__name__}: {exc}")
        print("  （搜索要联网，先把这条错误发我）")
        return
    if not tracks:
        print("  搜到 0 首，接口可能被限流")
        return
    for t in tracks:
        print(f"    {t.id}  {t.display}")
    print("  搜索正常。点歌时会挑最匹配的一条，用原生 IPC 插到下一首。")


def _check_netease_uia(cfg: Config) -> None:
    print("  （uia 模式需要 uiautomation；ipc 模式不需要）")
    try:
        from .music.netease import dump_tree, find_window_safe
    except Exception as exc:  # noqa: BLE001
        print("  跳过：", exc)
        return

    win = find_window_safe()
    if win is None:
        print("  没找到「网易云音乐」窗口。先把客户端打开再跑一次。")
        return

    from .music.netease import name_of, type_name

    print(f"  找到窗口：{name_of(win)!r} ({type_name(win)})")
    out = cfg.root / "logs" / "uia_tree.txt"
    count = dump_tree(win, out)
    print(f"  控件树已存到 {out}（{count} 个节点）")


def doctor(cfg: Config) -> None:
    print_env(cfg)
    check_vendor(cfg)
    check_room(cfg)
    check_netease(cfg)
    print()
    print("全部检查完。有问题把上面的输出发我。")
