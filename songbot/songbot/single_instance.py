"""保证同时只有一个实例在跑。

多开会导致：同一间房重复连接（抖音可能踢线）、同一条点歌被插两次、
日志互相覆盖。用 Windows 命名互斥体，进程退出自动释放，不会有残留锁。
"""

from __future__ import annotations

import ctypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183

_handle = None


def acquire(name: str = "songbot-singleton") -> bool:
    """拿到锁返回 True；已经有实例在跑返回 False。"""
    global _handle
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        return True  # 创建失败就别拦着，让程序继续跑
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True
