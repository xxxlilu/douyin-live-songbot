"""主流程：弹幕 → 解析 → 限流 → 交给网易云。"""

from __future__ import annotations

import logging

from .config import Config
from .danmaku import build_source
from .models import Danmaku
from .music import build_controller
from .parser import RequestParser
from .queue import SongQueue


def handle_one(
    dm: Danmaku,
    *,
    parser: RequestParser,
    queue: SongQueue,
    controller,
    logger: logging.Logger,
) -> bool:
    """处理一条弹幕；点歌成功（或已交给控制器）返回 True。"""
    req = parser.parse(dm)
    if req is None:
        logger.debug("不是点歌：%s", dm)
        return False

    ok, reason = queue.check(req)
    if not ok:
        logger.info("忽略 %s 的%s：%s", req.user, req.label(), reason)
        return False

    pushed = controller.add_to_next(
        req.keyword, match=req.song, artist=req.artist, user=req.user
    )
    queue.accept(req, pushed=pushed)

    if pushed:
        logger.info("✅ 已入队：%s 点 %s", req.user, req.label())
    else:
        logger.warning("⚠️ 入队失败：%s（%s 点的）", req.label(), req.user)
    return pushed


def run(cfg: Config, logger: logging.Logger) -> None:
    from .single_instance import acquire

    if not acquire():
        logger.error(
            "已经有一个 songbot 在跑了（任务管理器里能看到 python）。"
            "先把它关掉，否则会重复点歌、互相踢线。"
        )
        return

    parser = RequestParser(
        cfg.song.keywords,
        cfg.song.max_len,
        strict=cfg.song.strict,
        require_artist=cfg.song.require_artist,
    )
    # queue 模式下所有规则由 next5-overlay 执行，这里不能提前拦截
    if (cfg.netease.mode or "queue").lower() == "queue":
        queue = SongQueue(
            per_user_cooldown=0,
            per_user_max_pending=0,
            global_cooldown=0,
            duplicate_window=0,
            max_queue=1000,
        )
    else:
        queue = SongQueue(
            per_user_cooldown=cfg.song.per_user_cooldown_seconds,
            per_user_max_pending=cfg.song.per_user_max_pending,
            global_cooldown=cfg.song.global_cooldown_seconds,
            duplicate_window=cfg.song.duplicate_window_seconds,
            max_queue=cfg.song.max_queue,
        )
    controller = build_controller(cfg, logger)
    source = build_source(cfg, logger)

    logger.info("触发词：%s", " / ".join(cfg.song.keywords))
    logger.info("模式：%s", "dry_run（只识别不动网易云）" if cfg.general.dry_run else "真操作")
    logger.info("开始监听弹幕，Ctrl+C 退出")

    try:
        for dm in source.iter_danmaku():
            handle_one(dm, parser=parser, queue=queue, controller=controller, logger=logger)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，退出。%s", queue.stats())
    finally:
        source.close()
        controller.close()
