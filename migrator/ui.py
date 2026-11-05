"""终端等待动画（spinner）。

为什么不用 typer.progressbar：
    进度条需要已知总量才能算百分比，而本工具最耗时的两步
    （`git clone --mirror` 和 `push` 全部分支）拿不到任何进度信息，
    只能给「还在跑 + 已经跑了多久」这种反馈。

显示方式是「就地刷新 + 滚动落定」结合：

    ⠹ 镜像克隆 https://gitee.com/a/b.git  12.3s     <- 当前行，后台线程不断重画
    （下一次输出时这一行被定格，留在屏幕上）

具体规则：
- start(msg)  开一格动画，文字为 msg；
- log(msg)    先把上一行定格（补上它真实耗时），再让 msg 成为新的动画行，
              所以每行日志都自带「这一步花了多久」，不会重复也不会互相覆盖；
- stop(msg)   用 msg 替换当前行并定格，然后收线。

非交互环境（管道、CI、--no-spinner）会自动退化成普通逐行输出，
调用方不需要写任何 if。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from typing import Iterator, TextIO

import typer

# 盲文点阵，视觉上比 |/-\ 细腻；终端编码或字体不支持时退回 ASCII
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ASCII_FRAMES = "|/-\\"

# 耗时短于这个值的行不显示秒数，免得满屏 "0.0s"
_SHOW_ELAPSED_AFTER = 0.5
# 动画行上的秒数用整秒：0.1 秒跳一次数字看着太吵
_LIVE_ELAPSED_AFTER = 1.0

# 静音开关：--json 时打开，过程输出一个字都不写，只留结果
_QUIET = False


def _char_width(ch: str) -> int:
    """单个字符占几列：全角/宽字符（中文、emoji）按 2 列算。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _width(text: str) -> int:
    """字符串的显示宽度（不是 len，中文一个字占两列）。"""
    return sum(_char_width(ch) for ch in text)


def _clip(text: str, limit: int) -> str:
    """按显示宽度截断。

    动画靠 \\r 回到行首重画，文字一旦超过一行宽度就会折行，
    而 \\r 只能回到折行后的行首，屏幕上会留下擦不掉的残影，
    所以宁可截断也不能让它折行。
    """
    if _width(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for ch in text:
        w = _char_width(ch)
        if used + w > limit - 1:  # 留一列给省略号
            break
        kept.append(ch)
        used += w
    return "".join(kept) + "…"


class Spinner:
    """单行动画指示器。

    Args:
        stream: 写入目标，默认 stdout。不用 stderr 是因为「日志」和「动画」
            分属两个流时，重定向后顺序容易错乱。
        interval: 重绘间隔（秒）。0.1 看着顺滑又不浪费 CPU。
    """

    def __init__(self, stream: TextIO | None = None, *, interval: float = 0.1) -> None:
        self._stream = stream or sys.stdout
        self._interval = interval
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._halt = threading.Event()
        # 当前动画行的文字与颜色；None 表示没有活动行
        self._message: str | None = None
        self._color: str | None = None
        self._started = 0.0
        # 上一次实际写出的列数，用于把残留字符擦干净
        self._cols = 0
        self._frames = FRAMES
        self.enabled = self._autodetect()

    # ------------------------------------------------------------ 环境判断
    def _autodetect(self) -> bool:
        """判断当前能不能画动画。

        重定向到文件或管道时 isatty 为假；CI 里日志会被收集成纯文本，
        满屏 \\r 只会让日志变脏，也一并关掉。
        """
        if os.environ.get("NO_SPINNER") or os.environ.get("CI"):
            return False
        if os.environ.get("TERM", "") == "dumb":
            return False
        try:
            return bool(self._stream.isatty())
        except (AttributeError, ValueError):
            return False

    def _pick_frames(self) -> str:
        """按输出流编码挑帧：编不出来（如 cp936）就用 ASCII。"""
        if os.environ.get("SPINNER_ASCII"):
            return ASCII_FRAMES
        encoding = getattr(self._stream, "encoding", None) or "utf-8"
        try:
            FRAMES[0].encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return ASCII_FRAMES
        return FRAMES

    # ---------------------------------------------------------------- 控制
    def start(self, message: str = "", *, fg: str | None = None) -> Spinner:
        """开一格动画；已有动画时直接改文字（相当于切步骤）。"""
        if not self.enabled:
            # 无动画时退化成普通输出，这一行立刻打出来
            if message:
                self._write_line(message, fg)
            return self

        with self._lock:
            self._frames = self._pick_frames()
            self._message = message
            self._color = fg
            self._started = time.monotonic()
            if self._thread is None or not self._thread.is_alive():
                self._halt.clear()
                self._thread = threading.Thread(
                    target=self._spin, name="cli-spinner", daemon=True
                )
                self._thread.start()
        return self

    def update(self, message: str) -> None:
        """只替换当前行的文字，不落定（适合 `[2/5]` 这种计数）。"""
        with self._lock:
            if self.enabled and self._message is not None:
                self._message = message

    def log(self, message: str, *, fg: str | None = None) -> None:
        """输出一行日志，并把它变成新的动画行。

        上一行会带着自己的耗时定格，所以屏幕上的输出既是过程记录，
        也顺手是一张耗时表。
        """
        if not self.enabled:
            self._write_line(message, fg)
            return

        with self._lock:
            self._commit()
            self._message = message
            self._color = fg
            self._started = time.monotonic()

    def stop(self, message: str | None = None, *, fg: str | None = None) -> None:
        """定格当前行并停止动画。

        Args:
            message: 给了就用它替换当前行的文字（例如把「校验中」换成
                「✓ 校验通过」），None 表示按当前文字收线。
        """
        if not self.enabled:
            if message:
                self._write_line(message, fg)
            return

        with self._lock:
            if message is not None and self._message is not None:
                self._message = message
                if fg is not None:
                    self._color = fg
            self._commit()
            self._halt.set()
            thread, self._thread = self._thread, None

        # join 放在锁外：后台线程可能正卡在锁上等重绘
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    @property
    def active(self) -> bool:
        """当前是否有动画行在跑。"""
        return self._message is not None

    @contextmanager
    def working(self, message: str = "", *, fg: str | None = None) -> Iterator[Spinner]:
        """`with spinner.working("...")` 形式的糖，异常路径也会收线。"""
        self.start(message, fg=fg)
        try:
            yield self
        finally:
            self.stop()

    # ---------------------------------------------------------------- 绘制
    def _spin(self) -> None:
        """后台重绘循环，直到 stop() 置位 _halt。"""
        index = 0
        while not self._halt.is_set():
            with self._lock:
                if self._message is not None:
                    self._paint(self._frames[index % len(self._frames)])
            index += 1
            self._halt.wait(self._interval)

    def _paint(self, frame: str) -> None:
        """重画当前行（不换行，靠 \\r 回到行首）。"""
        if _QUIET:
            return
        message = (self._message or "").strip()
        elapsed = time.monotonic() - self._started
        text = f"{frame} {message}"
        if elapsed >= _LIVE_ELAPSED_AFTER:
            # 向下取整，秒数单调递增，不会出现 0.7s 显示成 "1s" 的跳变
            text = f"{text}  {int(elapsed)}s"

        limit = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        text = _clip(text, limit)
        # 补空格擦掉上一次更长的尾巴；_cols 记的是「实际写出去多少列」，
        # 否则下一次 _erase 会擦不干净，屏幕上留半行残影
        pad = max(0, self._cols - _width(text))
        self._stream.write("\r" + text + " " * pad)
        self._stream.flush()
        self._cols = _width(text) + pad

    def _erase(self) -> None:
        """把动画行整行擦掉，让位给普通输出。"""
        if self._cols:
            self._stream.write("\r" + " " * self._cols + "\r")
            self._stream.flush()
            self._cols = 0

    def _commit(self) -> None:
        """把当前行定格成一条普通输出（带该行总耗时）。"""
        if self._message is None:
            return

        message, color, started = self._message, self._color, self._started
        self._message = None
        self._erase()

        elapsed = time.monotonic() - started
        text = f"{message}  {elapsed:.1f}s" if elapsed >= _SHOW_ELAPSED_AFTER else message
        self._write_line(text, color)

    def _write_line(self, text: str, color: str | None) -> None:
        """走 typer 输出：颜色在 Windows 旧控制台上会被自动剥离。

        显式指定 file=self._stream，保证「动画行」和「定格行」写在同一个流上，
        否则重定向时两者会跑到不同地方，顺序全乱。
        """
        if _QUIET:
            return
        if color is None:
            # 不传 fg 时别用 secho：typer 仍会补一个 \x1b[0m，重定向时会写进文件
            typer.echo(text, file=self._stream)
        else:
            typer.secho(text, fg=color, file=self._stream)


# ------------------------------------------------------------------ 全局实例
# CLI 里到处都要用，做成单例；同时只存在一条动画行，也避免多个线程抢终端
spinner = Spinner()


def configure(enabled: bool) -> None:
    """CLI 级别的开关（--no-spinner / --json 时调用）。

    关掉时仍然看 isatty：`--no-spinner` 只负责「关」，不负责「强行开」，
    否则重定向到文件时会被塞进一堆 \\r。
    """
    spinner.enabled = bool(enabled) and spinner._autodetect()


def silence() -> None:
    """静音所有过程输出。

    `--json` 用：结果要能直接管道给 jq，任何 "  · 拉取仓库列表" 都是噪音，
    而且关掉动画也不够——普通模式照样会把这些步骤行打出来。
    """
    global _QUIET
    _QUIET = True


def start(message: str = "", *, fg: str | None = None) -> Spinner:
    """开一格动画，见 Spinner.start。"""
    return spinner.start(message, fg=fg)


def log(message: str, *, fg: str | None = None) -> None:
    """输出一行并继续动画，见 Spinner.log。"""
    spinner.log(message, fg=fg)


def stop(message: str | None = None, *, fg: str | None = None) -> None:
    """定格当前行并停止动画，见 Spinner.stop。"""
    spinner.stop(message, fg=fg)


def working(message: str = "", *, fg: str | None = None) -> Iterator[Spinner]:
    """`with working("..."):` 形式的糖。"""
    return spinner.working(message, fg=fg)
