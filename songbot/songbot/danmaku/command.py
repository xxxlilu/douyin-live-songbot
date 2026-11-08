"""跑一个外部命令，把它 stdout 的每一行当一条弹幕。

这样抖音协议怎么变都不影响主程序：坏的是那个外部脚本，换掉就行。
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from collections.abc import Iterator

from ..config import Config
from ..models import Danmaku
from .base import DanmakuSource
from .extract import extract, parse_line

_PYTHON_ALIASES = {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}


def direct_env() -> dict:
    """弹幕子进程不要走代理：抖音直连很快，走代理反而卡。"""
    import os

    env = os.environ.copy()
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
    ):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


class CommandDanmakuSource(DanmakuSource):
    def __init__(self, cfg: Config, logger) -> None:
        super().__init__(cfg, logger)
        self._proc: subprocess.Popen | None = None

    def _build_command(self) -> list[str]:
        template = (self.cfg.danmaku.command or "").strip()
        if not template:
            raise ValueError("config.ini 里 [danmaku] command 是空的")
        room = self.cfg.general.room_url.strip()
        if not room or room.endswith("000000000"):
            raise ValueError(
                "config.ini 里 [general] room_url 还没填你自己的直播间地址，"
                "例如 https://live.douyin.com/123456789"
            )
        cmd = template.replace("{room_url}", room)
        args = shlex.split(cmd, posix=False)
        # 配置里写 `python` 时，用当前解释器（venv 里那个），别让它去 PATH 里碰运气
        if args and args[0].lower() in _PYTHON_ALIASES:
            args[0] = sys.executable
        return args

    def iter_danmaku(self) -> Iterator[Danmaku]:
        args = self._build_command()
        self.logger.info("弹幕来源：%s", " ".join(args))

        while True:
            self.logger.info("启动弹幕进程…")
            try:
                self._proc = subprocess.Popen(
                    args,
                    env=direct_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except FileNotFoundError as exc:
                self.logger.error("启动失败：%s（检查 command 里那个脚本/解释器路径）", exc)
                raise

            try:
                yield from self._read_lines()
            finally:
                self._kill()

            wait = max(1.0, self.cfg.danmaku.reconnect_seconds)
            self.logger.warning("弹幕流断了，%.0f 秒后重连", wait)
            time.sleep(wait)

    def _read_lines(self) -> Iterator[Danmaku]:
        assert self._proc is not None
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            obj = parse_line(line)
            if obj is None:
                text = line.rstrip()[:500]
                if "WebSocket连接成功" in text:
                    self.logger.info("弹幕已连上，开始监听（发一条「点歌 歌名」试试）")
                elif "【X】" in text or "[X]" in text:
                    # 库的报错要让主播看见，其余启动日志留在 debug
                    self.logger.warning("[弹幕进程] %s", text)
                else:
                    self.logger.debug("[弹幕进程] %s", text)
                continue
            item = extract(obj)
            if item is None:
                self.logger.debug("[跳过非评论] %s", line.rstrip()[:300])
                continue
            yield Danmaku(user=item.user or "匿名观众", content=item.content, raw=obj)

    def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def close(self) -> None:
        self._kill()
