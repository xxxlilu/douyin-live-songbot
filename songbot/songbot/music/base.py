"""控制器公共接口。"""

from __future__ import annotations

import logging


class BaseController:
    def add_to_next(
        self, query: str, match: str | None = None, artist: str = "", user: str = ""
    ) -> bool:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass


class DryRunController(BaseController):
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def add_to_next(
        self, query: str, match: str | None = None, artist: str = "", user: str = ""
    ) -> bool:
        self.logger.info("[dry_run] 本应入队：%s 点《%s》", user or "某观众", query)
        return True
