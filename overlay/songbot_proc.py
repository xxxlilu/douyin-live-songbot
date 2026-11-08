"""把 songbot 作为子进程拉起来/停掉，并把它的日志喂给控制面板。

关键点：用 Windows Job Object 把子进程绑在父进程上，父进程一退出
（哪怕是崩溃），子进程也会被系统一起收掉，不会留下孤儿占着单实例锁。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path

from connector import clean_env

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

LARGE_INTEGER = ctypes.c_longlong
ULONG_PTR = ctypes.c_size_t


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_job = None


def _attach_to_job(handle: int) -> bool:
    """创建（一次）一个 kill-on-close 的 Job，把子进程放进去。"""
    global _job
    try:
        if _job is None:
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info),
            )
            if not ok:
                kernel32.CloseHandle(job)
                return False
            _job = job
        return bool(kernel32.AssignProcessToJobObject(_job, handle))
    except Exception:
        return False


def _stamp(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


class SongbotProcess:
    def __init__(self, cfg: dict, logger=print) -> None:
        conf = cfg.get("songbot") or {}
        self.log = logger
        self.auto_start = bool(conf.get("auto_start", True))
        self.python = Path()
        self.cwd = Path()
        self.args: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._lines: deque[str] = deque(maxlen=120)
        self._lock = threading.Lock()
        self.connected = False
        self.started_at = 0.0
        self.last_line_at = 0.0
        self.last_enqueue_at = 0.0
        self.enqueued = 0
        self.error = ""

    # ---------------------------------------------------------------- 配置
    def configure(self, cfg: dict, root: Path) -> None:
        conf = cfg.get("songbot") or {}
        py = conf.get("python") or "..\\songbot\\.venv\\Scripts\\python.exe"
        cwd = conf.get("cwd") or "..\\songbot"
        args = conf.get("args") or ["-m", "songbot", "run"]
        self.python = (root / py).resolve()
        self.cwd = (root / cwd).resolve()
        self.args = list(args)
        self.auto_start = bool(conf.get("auto_start", True))

    # ---------------------------------------------------------------- 状态
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        with self._lock:
            lines = list(self._lines)[-12:]
        return {
            "running": self.running(),
            "pid": self._proc.pid if self.running() else None,
            "connected": self.connected,
            "started_at": _stamp(self.started_at),
            "last_line_at": _stamp(self.last_line_at),
            "last_enqueue_at": _stamp(self.last_enqueue_at),
            "enqueued": self.enqueued,
            "auto_start": self.auto_start,
            "error": self.error,
            "lines": lines,
        }

    # ---------------------------------------------------------------- 控制
    def start(self) -> tuple[bool, str]:
        if self.running():
            return False, "弹幕监听已经在跑了"
        if not self.python.is_file():
            self.error = f"找不到 python：{self.python}"
            return False, self.error
        if not self.cwd.is_dir():
            self.error = f"找不到 songbot 目录：{self.cwd}"
            return False, self.error

        try:
            self._proc = subprocess.Popen(
                [str(self.python), "-u", *self.args],
                cwd=str(self.cwd),
                env=clean_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            self.error = f"启动失败：{exc}"
            return False, self.error

        _attach_to_job(int(self._proc._handle))  # noqa: SLF001
        self.started_at = time.time()
        self.connected = False
        self.error = ""
        with self._lock:
            self._lines.clear()
        threading.Thread(target=self._pump, daemon=True).start()
        self.log("[songbot] 已启动，pid=%s", self._proc.pid)
        return True, "弹幕监听已启动"

    def stop(self) -> tuple[bool, str]:
        proc, self._proc = self._proc, None
        self.connected = False
        if proc is None or proc.poll() is not None:
            return True, "弹幕监听没在跑"
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self.log("[songbot] 已停止")
        return True, "弹幕监听已停止"

    def restart(self) -> tuple[bool, str]:
        self.stop()
        time.sleep(0.6)
        return self.start()

    # ---------------------------------------------------------------- 读日志
    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            now = time.time()
            with self._lock:
                self._lines.append(line[:300])
            self.last_line_at = now
            self._interpret(line)
        with self._lock:
            self._lines.append("—— 进程已退出 ——")
        self.connected = False
        code = proc.poll()
        if code not in (None, 0):
            self.error = f"songbot 退出，错误码 {code}"
            self.log("[songbot] %s", self.error)

    def _interpret(self, line: str) -> None:
        if "弹幕已连上" in line:
            self.connected = True
            self.error = ""
        elif "弹幕流断了" in line:
            self.connected = False
        elif "已入队" in line:
            self.enqueued += 1
            self.last_enqueue_at = time.time()
        elif "已经有一个 songbot 在跑了" in line:
            self.error = "已经有一个 songbot 在跑（可能是之前留下的）"
