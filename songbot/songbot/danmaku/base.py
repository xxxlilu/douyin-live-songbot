"""弹幕来源的公共接口。

想换弹幕来源（第三方库、OCR、开放平台接口）时，只要实现 iter() 就行，
主程序完全不关心下面是怎么来的。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from ..config import Config
from ..models import Danmaku


class DanmakuSource:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger

    def iter_danmaku(self) -> Iterator[Danmaku]:  # pragma: no cover - 接口
        raise NotImplementedError

    def close(self) -> None:
        pass


def build_source(cfg: Config, logger: logging.Logger) -> DanmakuSource:
    source = (cfg.danmaku.source or "command").lower()
    if source == "command":
        from .command import CommandDanmakuSource

        return CommandDanmakuSource(cfg, logger)
    if source == "vendor":
        from .vendor import VendorDanmakuSource

        return VendorDanmakuSource(cfg, logger)
    raise ValueError(f"未知的弹幕来源：{cfg.danmaku.source}（只支持 command / vendor）")
