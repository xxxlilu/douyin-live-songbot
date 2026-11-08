"""音乐控制层。

按 `[netease] mode` 选后端：

- queue（默认）：只把点歌写进 next5-overlay 的收件箱，由那边排队和投放
- awoo：直接驱动嗷呜连接器插入「下一首」（旧流程，保留备用）
- ipc：老的 orpheus 窗口消息（网易云 3.1.41+ 已失效）
- uia：界面自动化兜底
"""

from __future__ import annotations

import logging

from ..config import Config
from .base import BaseController, DryRunController

__all__ = ["build_controller", "BaseController", "DryRunController"]


def build_controller(cfg: Config, logger: logging.Logger) -> BaseController:
    if cfg.general.dry_run:
        return DryRunController(logger)

    mode = (cfg.netease.mode or "queue").lower()
    if mode == "queue":
        from .queue_out import QueueController

        return QueueController(cfg, logger)
    if mode == "uia":
        from .netease import NeteaseController

        return NeteaseController(cfg, logger)
    if mode == "ipc":
        from .netease_ipc import NeteaseIpcController

        return NeteaseIpcController(cfg, logger)

    from .awoo import AwooController

    return AwooController(cfg, logger)
