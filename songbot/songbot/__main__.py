"""命令行入口：python -m songbot [run|test|doctor|tree|dump]"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .app import handle_one, run
from .config import load_config
from .log import setup_logging
from .models import Danmaku
from .music import build_controller
from .parser import RequestParser
from .queue import SongQueue

HERE = Path(__file__).resolve().parent.parent


def _load(args) -> tuple:
    cfg = load_config(args.config or (HERE / "config.ini"))
    log_path = Path(cfg.general.log_file)
    if not log_path.is_absolute():
        log_path = cfg.root / log_path
    logger = setup_logging(log_path, verbose=args.verbose)
    return cfg, logger


def cmd_run(args) -> int:
    cfg, logger = _load(args)
    run(cfg, logger)
    return 0


def cmd_test(args) -> int:
    """不接弹幕，直接喂一条假弹幕，验证「解析 + 网易云操作」这一整条链。"""
    cfg, logger = _load(args)
    parser = RequestParser(
        cfg.song.keywords,
        cfg.song.max_len,
        strict=cfg.song.strict,
        require_artist=cfg.song.require_artist,
    )
    queue = SongQueue(
        per_user_cooldown=0,
        per_user_max_pending=0,
        global_cooldown=0,
        duplicate_window=0,
        max_queue=cfg.song.max_queue,
    )
    controller = build_controller(cfg, logger)
    text = args.text or "点歌 晴天"
    print(f"输入弹幕：{text}")
    dm = Danmaku(user="测试观众", content=text, ts=datetime.now())
    ok = handle_one(dm, parser=parser, queue=queue, controller=controller, logger=logger)
    print("结果：", "成功" if ok else "没走通（看上面日志）")
    return 0 if ok else 1


def cmd_dump(args) -> int:
    """把弹幕命令的原始输出照原样打出来。

    第三方库吐的到底是 JSON、Python dict 还是人话，一眼就知道，
    也方便确认它有没有真的连上直播间。
    """
    cfg, logger = _load(args)
    from .danmaku.command import CommandDanmakuSource, direct_env

    source = CommandDanmakuSource(cfg, logger)
    argv = source._build_command()
    print("命令：", " ".join(argv))
    print(f"最多看 {args.seconds:.0f} 秒 / {args.lines} 行，Ctrl+C 结束\n")

    proc = subprocess.Popen(
        argv,
        env=direct_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    deadline = time.monotonic() + args.seconds
    shown = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"RAW> {line[:400]}")
                shown += 1
            if shown >= args.lines or time.monotonic() > deadline:
                break
    except KeyboardInterrupt:
        print("\n（手动结束）")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    if shown == 0:
        print("一行输出都没拿到——要么命令启动就失败，要么这个库不打印。")
        return 1
    print(f"\n共看到 {shown} 行。把这些发我，我按真实格式接线。")
    return 0


def cmd_doctor(args) -> int:
    cfg, _logger = _load(args)
    from .doctor import doctor

    doctor(cfg)
    return 0


def cmd_netease(args) -> int:
    """诊断「搜歌 + 插到下一首」这条链路，每一步都打印。"""
    cfg, _logger = _load(args)
    mode = (cfg.netease.mode or "awoo").lower()
    keyword = " ".join(args.keyword) if args.keyword else None
    print(f"后端 mode = {mode}")

    if mode == "awoo":
        from .music.awoo import diagnose_awoo

        print(diagnose_awoo(cfg, keyword=keyword, do_insert=args.insert))
        return 0

    from .music import netease_ipc

    print(netease_ipc.diagnose(keyword=keyword, do_insert=args.insert, brute=args.brute))
    return 0


def cmd_tree(args) -> int:
    cfg, logger = _load(args)
    from .music.netease import dump_tree, find_window_safe

    win = find_window_safe()
    if win is None:
        print("没找到网易云窗口，先打开客户端")
        return 1
    out = cfg.root / "logs" / "uia_tree.txt"
    dump_tree(win, out, logger=logger, max_depth=args.depth)
    print(f"已写入 {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="songbot", description="抖音弹幕点歌 → 网易云下一首播放")
    p.add_argument("-c", "--config", help="配置文件路径（默认项目下的 config.ini）")
    p.add_argument("-v", "--verbose", action="store_true", help="多打点日志")
    p.add_argument("--version", action="version", version=f"songbot {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("run", help="正式跑：连弹幕，自动点歌")

    t = sub.add_parser("test", help="喂一条假弹幕，测整条链")
    t.add_argument("text", nargs="?", default="点歌 晴天")

    sub.add_parser("doctor", help="自检依赖 / 配置 / 网易云窗口")

    tr = sub.add_parser("tree", help="把网易云控件树导出来")
    tr.add_argument("--depth", type=int, default=12)

    d = sub.add_parser("dump", help="看弹幕命令的原始输出（查格式用）")
    d.add_argument("--seconds", type=float, default=30)
    d.add_argument("--lines", type=int, default=50)

    n = sub.add_parser("netease", help="诊断「搜歌 + 插到下一首」这条链路")
    n.add_argument("keyword", nargs="*", help="要搜的歌名（可以有空格，如 SOS Avicii）")
    n.add_argument("--insert", action="store_true", help="真的插一次（会改播放队列）")
    n.add_argument("--brute", action="store_true", help="投递成功但没反应时，逐个试外层命令号（会切歌）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "run"
    handlers = {
        "run": cmd_run,
        "test": cmd_test,
        "doctor": cmd_doctor,
        "tree": cmd_tree,
        "dump": cmd_dump,
        "netease": cmd_netease,
    }
    return handlers[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
