"""把 vendor/DouyinLiveWebFetcher 当库直接调用（实验性）。

上游库的入口签名这几年改过好几次，这里靠反射去猜，猜不中就明确报错，
让你回退到 [danmaku] source=command（那条路永远是能用的）。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from ..config import Config
from ..models import Danmaku
from .base import DanmakuSource
from .extract import extract

DEFAULT_PATH = Path("vendor") / "DouyinLiveWebFetcher"


class VendorDanmakuSource(DanmakuSource):
    def __init__(self, cfg: Config, logger) -> None:
        super().__init__(cfg, logger)
        self.vendor_dir = (cfg.root / DEFAULT_PATH).resolve()
        self._queue: list[Danmaku] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ---------- 加载 ----------
    def _load_module(self):
        candidates = [
            self.vendor_dir / "liveWebFetcher.py",
            self.vendor_dir / "live_web_fetcher.py",
        ]
        target = next((p for p in candidates if p.is_file()), None)
        if target is None:
            raise FileNotFoundError(
                f"在 {self.vendor_dir} 里找不到 liveWebFetcher.py。\n"
                "请先克隆：git clone https://github.com/saermart/DouyinLiveWebFetcher vendor/DouyinLiveWebFetcher\n"
                "或者把 config.ini 的 [danmaku] source 改回 command。"
            )
        sys.path.insert(0, str(self.vendor_dir))
        spec = importlib.util.spec_from_file_location("vendor_live_web_fetcher", target)
        if spec is None or spec.loader is None:
            raise ImportError(f"加载失败：{target}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _on_message(self, message) -> None:
        item = extract(message)
        if item is None:
            return
        with self._lock:
            self._queue.append(Danmaku(user=item.user or "匿名观众", content=item.content, raw=message))

    # ---------- 主流程 ----------
    def iter_danmaku(self) -> Iterator[Danmaku]:
        module = self._load_module()
        cls = getattr(module, "DouyinLiveWebFetcher", None)
        if cls is None:
            raise AttributeError("vendor 库里没有 DouyinLiveWebFetcher 这个类，用 command 模式吧")

        room = self.cfg.general.room_url.strip()
        instance = cls(room)

        hooked = False
        for name in ("on_message", "callback", "handler", "on_danmaku"):
            if hasattr(instance, name):
                setattr(instance, name, self._on_message)
                hooked = True
                break
        if not hooked:
            self.logger.warning(
                "vendor 库没有回调接口，收不到消息；建议改回 [danmaku] source=command"
            )

        thread = threading.Thread(target=self._run_instance, args=(instance,), daemon=True)
        thread.start()

        while thread.is_alive() or self._queue:
            with self._lock:
                batch, self._queue = self._queue, []
            for dm in batch:
                yield dm
            self._stop.wait(0.2)

    def _run_instance(self, instance) -> None:
        try:
            entry = None
            for name in ("start", "run", "start_websocket", "_connectWebSocket"):
                if hasattr(instance, name):
                    entry = getattr(instance, name)
                    break
            if entry is None:
                self.logger.error("vendor 库里找不到可调用的启动方法")
                return
            if inspect.iscoroutinefunction(entry):
                import asyncio

                asyncio.run(entry())
            else:
                result = entry()
                if inspect.isawaitable(result):
                    import asyncio

                    asyncio.run(result)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("弹幕库异常退出：%s: %s", type(exc).__name__, exc)

    def close(self) -> None:
        self._stop.set()
