import time

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    track,
)

from migrator import ui


def test_basic():
    console = Console()
    console.print("普通文本")
    console.print("红色文本", style="red")
    console.print("粗体+下划线+青色", style="bold underline cyan")
    console.print("[bold magenta]富文本标记[/bold magenta] 也可以这样用")
    console.print("带背景色", style="white on blue")
    console.print("⚠️ 警告信息", style="bold yellow")
    console.print("✅ 成功信息", style="bold green")

    for i in track(range(10), description="loading"):
        time.sleep(0.1)
        print("i")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("connect server", total=None)
        time.sleep(1)
        progress.update(task, description="verify identify")
        time.sleep(1)
        progress.update(task, description="update")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}", justify="right"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),  # 显示 3/100
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),  # 已用时间
        TimeRemainingColumn(),  # 剩余时间
        console=console,
    ) as progress:
        task = progress.add_task("下载文件", total=100)
        for _ in range(100):
            time.sleep(0.03)
            progress.update(task, advance=1)

    with Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task1 = progress.add_task("下载 A.zip", total=80)
        task2 = progress.add_task("下载 B.zip", total=120)
        task3 = progress.add_task("解压文件", total=50)

        while not progress.finished:
            progress.update(task1, advance=1)
            progress.update(task2, advance=1)
            progress.update(task3, advance=1)
            time.sleep(0.02)


def test_ui():
    ui.start("ok")
    time.sleep(10)
    ui.start("end ok")
