"""操作网易云音乐客户端：搜歌 → 右键第一条 → 「下一首播放」。

为什么是界面自动化：网易云客户端没有公开的本地接口能把歌插进「下一首」，
播放队列是客户端本地的状态，只有点它自己的菜单才真的生效。

不依赖具体控件树写死路径：这里用「遍历 + 按名字打分」的方式找控件，
换皮肤/改版时通常还能命中；实在不行就靠 `python -m songbot tree` 打出来的树来调。
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time

from ..config import Config

MAX_NODES = 4000
MAX_DEPTH = 14


class UiaUnavailable(RuntimeError):
    pass


def _auto():
    try:
        import uiautomation as auto  # 延迟导入，dry_run 时就不用装
    except Exception as exc:  # noqa: BLE001
        raise UiaUnavailable(
            "没装 uiautomation。请在项目目录运行：python -m pip install -r requirements.txt"
        ) from exc
    try:
        auto.SetGlobalSearchTimeout(2)
    except Exception:
        pass
    return auto


# ---------------------------------------------------------------- 剪贴板
def set_clipboard(text: str) -> bool:
    """把文字放进剪贴板（中文只能靠粘贴，SendKeys 打不出汉字）。"""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        data = (text + "\0").encode("utf-16-le")
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
        finally:
            user32.CloseClipboard()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 遍历控件树
def walk(ctrl, logger=None, max_nodes: int = MAX_NODES, max_depth: int = MAX_DEPTH):
    """自己写 BFS，比依赖各种 find 接口更可控。"""
    stack = [(ctrl, 0)]
    seen = 0
    while stack and seen < max_nodes:
        node, depth = stack.pop()
        seen += 1
        yield node, depth
        if depth >= max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            continue
        for child in children:
            stack.append((child, depth + 1))


def type_name(ctrl) -> str:
    try:
        return ctrl.ControlTypeName or ""
    except Exception:
        return ""


def name_of(ctrl) -> str:
    try:
        return (ctrl.Name or "").strip()
    except Exception:
        return ""


def center_of(ctrl) -> tuple[int, int] | None:
    try:
        point = ctrl.GetClickablePoint()
        return (int(point[0]), int(point[1]))
    except Exception:
        return None


def dump_tree(ctrl, path, logger=None, max_depth: int = 12, max_nodes: int = 6000) -> int:
    lines: list[str] = []
    count = 0
    for node, depth in walk(ctrl, max_depth=max_depth, max_nodes=max_nodes):
        count += 1
        indent = "  " * depth
        lines.append(
            f"{indent}{type_name(node):<20} name={name_of(node)!r} "
            f"cls={getattr(node, 'ClassName', '')!r} id={getattr(node, 'AutomationId', '')!r}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    if logger:
        logger.info("控件树已写入 %s（%d 个节点）", path, count)
    return count


def find_window_safe():
    """不抛异常地找网易云窗口，给 doctor 用。"""
    try:
        auto = _auto()
    except UiaUnavailable:
        return None
    return _find_window(auto)


def _find_window(auto):
    root = auto.GetRootControl()
    best = None
    try:
        children = root.GetChildren()
    except Exception:
        children = []
    for child in children:
        nm = name_of(child)
        if "网易云音乐" in nm:
            return child
        cls = (getattr(child, "ClassName", "") or "").lower()
        if nm and best is None and "cloudmusic" in cls:
            best = child
    return best


# ---------------------------------------------------------------- 控制器
from .base import BaseController, DryRunController  # noqa: E402


class NeteaseController(BaseController):
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger
        self.auto = _auto()
        self._win = None

    # ---------------- 窗口 ----------------
    def find_window(self):
        return _find_window(self.auto)

    def ensure_window(self, launch_if_missing: bool = True):
        win = self.find_window()
        if win is not None:
            return win

        exe = self.cfg.netease.executable
        if not launch_if_missing:
            return None
        self.logger.info("网易云没开，尝试启动：%s", exe)
        try:
            subprocess.Popen([exe], cwd=str(__import__("pathlib").Path(exe).parent))
        except Exception as exc:  # noqa: BLE001
            self.logger.error("启动网易云失败：%s", exc)
            return None

        for _ in range(25):
            time.sleep(1)
            win = self.find_window()
            if win is not None:
                self.logger.info("网易云已启动")
                return win
        self.logger.error("等了 25 秒没等到网易云窗口")
        return None

    def _activate(self, win) -> None:
        try:
            win.SetActive()
        except Exception:
            try:
                win.SetFocus()
            except Exception:
                pass
        time.sleep(0.3)

    # ---------------- 搜歌 ----------------
    def _find_search_box(self, win):
        # 1) 优先按占位文案找
        for label in ("搜索音乐、视频、电台", "搜索音乐", "搜索"):
            try:
                edit = win.EditControl(Name=label, searchDepth=16)
                if edit.Exists(0):
                    return edit
            except Exception:
                continue
        # 2) 退一步：第一个 Edit 控件
        for node, _depth in walk(win, max_depth=14, max_nodes=1500):
            if type_name(node) == "EditControl":
                return node
        return None

    def _search(self, win, keyword: str) -> bool:
        edit = self._find_search_box(win)
        if edit is None:
            self.logger.error("没找到搜索框（先跑 `python -m songbot tree` 看控件树）")
            return False

        try:
            edit.Click()
        except Exception:
            point = center_of(edit)
            if point:
                self.auto.Click(point[0], point[1])

        if set_clipboard(keyword):
            self.auto.SendKeys("{Ctrl}a", waitTime=0.1)
            self.auto.SendKeys("{Ctrl}v", waitTime=0.1)
        else:
            self.auto.SendKeys(keyword, waitTime=0.1)

        self.auto.SendKeys("{Enter}", waitTime=0.2)
        time.sleep(max(0.5, self.cfg.netease.search_wait_seconds))
        return True

    def _click_single_tab(self, win) -> None:
        """搜索结果里点「单曲」标签，避免点到歌单/歌手。"""
        for node, _depth in walk(win, max_depth=12, max_nodes=2500):
            if type_name(node) in ("TextControl", "ButtonControl", "ListItemControl"):
                if name_of(node) in ("单曲", "歌曲"):
                    point = center_of(node)
                    if point:
                        try:
                            node.Click()
                        except Exception:
                            self.auto.Click(point[0], point[1])
                        time.sleep(1.0)
                        return

    @staticmethod
    def _score(item_name: str, keyword: str) -> int:
        a = "".join(item_name.split()).lower()
        b = "".join(keyword.split()).lower()
        if not a or not b:
            return 0
        if b in a:
            return 2
        # 至少两个连续字重合
        for i in range(len(b) - 1):
            if b[i : i + 2] in a:
                return 1
        return 0

    def _find_best_result(self, win, keyword: str):
        best = None
        best_score = 0
        for node, _depth in walk(win, max_depth=13, max_nodes=3000):
            if type_name(node) != "ListItemControl":
                continue
            nm = name_of(node)
            score = self._score(nm, keyword)
            if score > best_score:
                best_score, best = score, node
            if len(nm) < 2:
                continue

        if best is None:
            # 没有列表项，退一步找名字含关键词的文本
            for node, _depth in walk(win, max_depth=13, max_nodes=3000):
                if type_name(node) in ("TextControl", "ButtonControl") and self._score(name_of(node), keyword) == 2:
                    return node
        return best

    def _right_click_menu(self, node) -> bool:
        action = self.cfg.netease.action
        point = center_of(node)
        if point is None:
            self.logger.error("拿不到结果条目的坐标")
            return False

        try:
            self.auto.RightClick(point[0], point[1])
        except Exception:
            try:
                node.RightClick()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("右键失败：%s", exc)
                return False

        time.sleep(0.6)
        for item, _depth in walk(self.auto.GetRootControl(), max_depth=10, max_nodes=2000):
            if type_name(item) == "MenuItemControl" and name_of(item) == action:
                try:
                    item.Click()
                except Exception:
                    p = center_of(item)
                    if p is None:
                        return False
                    self.auto.Click(p[0], p[1])
                return True
        return False

    # ---------------- 对外 ----------------
    def add_to_next(
        self, query: str, match: str | None = None, artist: str = "", user: str = ""
    ) -> bool:
        keyword = match or query
        win = self.ensure_window()
        if win is None:
            return False

        self._activate(win)
        search_text = query + (self.cfg.netease.search_suffix or "")

        if not self._search(win, search_text):
            return False
        if self.cfg.netease.prefer_single_tab:
            self._click_single_tab(win)

        result = self._find_best_result(win, keyword)
        if result is None:
            self.logger.warning("搜索结果里没找到《%s》", keyword)
            return False

        self.logger.debug("命中结果条目：%s", name_of(result))
        if not self._right_click_menu(result):
            self.logger.warning("右键菜单里没找到「%s」", self.cfg.netease.action)
            return False
        self.logger.debug("已点击「%s」", self.cfg.netease.action)
        return True
