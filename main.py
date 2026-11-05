#!/usr/bin/env python3
"""reposor-trans 入口。

只做一件事：把命令行参数交给 migrator.cli.main，并把返回的退出码透传给 shell。
真正的逻辑都在 migrator 包里，这样 `python main.py` 和安装后的
`reposor-trans` 命令走的是同一套代码。

用法见 README.md，或运行：python main.py --help
"""

import sys

from migrator.cli import main

if __name__ == "__main__":
    sys.exit(main())
