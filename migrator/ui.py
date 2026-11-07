"""终端等待动画，基于 rich.progress。

为什么用 rich 而不是 typer.progressbar：
    typer 的进度条要求已知总量才能算百分比，而本工具最耗时的两步
    （`git clone --mirror` 和 `push` 全部分支）拿不到任何进度信息。
    rich 的 Progress 支持 total=None 的「不确定进度」，SpinnerColumn +
    TimeElapsedColumn 正好就是「在转 + 已经跑了多久」。

显示方式是「一行原地刷新 + 滚动落定」：

    ⠹ 镜像克隆 https://gitee.com/a/b.git  0:08     <- live 区，rich 负责重绘
    （下一步开始时，这一行被打印成普通文本并补上耗时）

具体规则：
- start(msg)  开始一格动画，文字为 msg；
- log(msg)    先把上一行落定（带上它真实耗时），再让 msg 成为新的动画行，
              所以每行日志都自带「这一步花了多久」，不会重复也不会互相覆盖；
- stop(msg)   用 msg 替换当前行并落定，然后收线。

光标移动、擦除、终端宽度、编码、Windows 控制台适配全部交给 rich，
这里只负责「什么时候该落定哪一行」。非交互环境（管道、CI、--no-spinner）
自动退化成普通逐行输出，调用方不需要写任何 if。
"""

from __future__ import annotations

import os
import sys
import threading
import time

from rich.console import Console
from rich.progress import Progress, ProgressColumn, SpinnerColumn, TextColumn
from rich.text import Text

# 耗时短于这个值的行不显示秒数，免得满屏 "0.0s"
_SHOW_ELAPSED_AFTER = 0.5

# 静音开关：--json 时打开，过程输出一个字都不写，只留结果
_QUIET = False


class SecondsColumn(ProgressColumn):
    """把已用时间显示成 `8.4s`。

    rich 自带的 TimeElapsedColumn 是 `0:00:08` 这种时分秒格式，
    和落定行里的 `8.4s` 摆在一起会显得两套风格，这里统一成秒。
    只实现 render 一个方法，刷新节奏、宽度计算仍然由 rich 接管。
    """

    def render(self, task) -> Text:
        return Text(f"{task.elapsed or 0:.1f}s", style="progress.elapsed")


def _autodetect() -> bool:
    """判断当前能不能画动画。

    重定向到文件或管道时 isatty 为假；CI 里日志会被收集成纯文本，
    满屏控制字符只会让日志变脏，也一并关掉。
    """
    if os.environ.get("NO_SPINNER") or os.environ.get("CI"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class Spinner:
    """单行动画指示器，内部就是一条 total 未知的 Progress。

    全局复用一个实例：同时只该有一个动画，多个线程抢终端没有意义。
    """

    def __init__(self, console: Console | None = None) -> None:
        # 日志里会出现 "[1/2]" 这种方括号，不能被当成富文本标签
        # （markup=False 也可以挡掉用户仓库名里带的方括号）
        self._console = console or Console(
            markup=False, highlight=False, soft_wrap=False
        )
        self.enabled = _autodetect()
        self._progress = Progress(
            SpinnerColumn(self._spinner_name()),
            TextColumn("{task.description}", markup=False),
            SecondsColumn(),
            console=self._console,
            # 收线时擦掉 live 区：历史信息已经由我们打印成普通行了
            transient=True,
            # 不让 rich 接管 sys.stdout，输出顺序保持可控
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._lock = threading.RLock()
        self._task: int | None = None
        self._message: str | None = None
        self._color: str | None = None
        self._started = 0.0

    @staticmethod
    def _spinner_name() -> str:
        """字体显示不出盲文点时，用 ASCII 的 -\\|/ 代替。"""
        return "line" if os.environ.get("SPINNER_ASCII") else "dots"

    # ---------------------------------------------------------------- 控制
    def start(self, message: str = "", *, fg: str | None = None) -> Spinner:
        """开始（或切换）一个动画行。"""
        if not self.enabled:
            # 无动画时退化成普通输出，这一行立刻打出来
            if message:
                self._write_line(message, fg)
            return self

        with self._lock:
            task = self._ensure_task()
            self._message = message
            self._color = fg
            self._started = time.monotonic()
            # reset 会把计时器归零，SecondsColumn 从当前这步重新计；
            # strip 是为了让动画行顶格，日志里的缩进只保留在落定后的输出里
            self._progress.reset(task, description=message.strip())
        return self

    def update(self, message: str) -> None:
        """只替换当前行的文字，不落定（适合 `[2/5]` 这类计数）。"""
        with self._lock:
            if self.enabled and self._message is not None and self._task is not None:
                self._message = message
                self._progress.update(self._task, description=message.strip())

    def log(self, message: str, *, fg: str | None = None) -> None:
        """输出一行日志，并把它变成新的动画行。

        上一行会带着自己的耗时落定，所以屏幕上的输出既是过程记录，
        也顺手是一张耗时表。
        """
        if not self.enabled:
            self._write_line(message, fg)
            return

        with self._lock:
            self._flush()
            self._message = message
            self._color = fg
            self._started = time.monotonic()
            if self._task is not None:
                self._progress.reset(self._task, description=message.strip())

    def stop(self, message: str | None = None, *, fg: str | None = None) -> None:
        """落定当前行并停止动画。

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
            self._flush()
            self._progress.stop()
            if self._task is not None:
                self._progress.remove_task(self._task)
                self._task = None

    @property
    def active(self) -> bool:
        """当前是否有动画行在跑。"""
        return self._message is not None

    # ---------------------------------------------------------------- 内部
    def _ensure_task(self) -> int:
        """懒启动 Progress：真要画动画时才占一个 live 区。"""
        if self._task is None:
            self._task = self._progress.add_task("", total=None)
            self._progress.start()
        return self._task

    def _flush(self) -> None:
        """把当前行落定成普通输出（带该行总耗时）。"""
        if self._message is None:
            return

        message, color, started = self._message, self._color, self._started
        self._message = None

        elapsed = time.monotonic() - started
        text = (
            f"{message}  {elapsed:.1f}s" if elapsed >= _SHOW_ELAPSED_AFTER else message
        )
        self._write_line(text, color)

    def _write_line(self, text: str, color: str | None) -> None:
        if _QUIET:
            return
        # 非终端时 rich 会自动剥掉颜色，Windows 控制台的 ANSI 也由它处理
        self._console.print(text, style=color or None, markup=False)


# ------------------------------------------------------------------ 全局实例
spinner = Spinner()


def configure(enabled: bool) -> None:
    """CLI 级别的开关（--no-spinner / --json 时调用）。

    关掉时仍然看 isatty：`--no-spinner` 只负责「关」，不负责「强行开」，
    否则重定向到文件时会被塞进一堆控制字符。
    """
    spinner.enabled = bool(enabled) and _autodetect()


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
    """落定当前行并停止动画，见 Spinner.stop。"""
    spinner.stop(message, fg=fg)
