"""加载 .env 配置，底层用 python-dotenv。

只保留一个入口 load_env_file()，调用方不关心 dotenv 的细节：
- 默认读当前目录 .env，找不到就沿父目录向上找（在子目录里执行也能生效）
- 显式传 --env-file 时只认这个路径，不会回退
- override=False：已经存在的环境变量优先，这样
  `GITHUB_TOKEN=xxx python main.py ...` 的临时指定不会被文件里的值盖掉
"""

from __future__ import annotations

from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _resolve_path(path: str | Path | None) -> Path | None:
    """确定要加载的 .env 路径。

    显式指定就只认它；否则先看当前目录，再向上查找。
    """
    if path:
        return Path(path).expanduser()

    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return cwd_env

    # usecwd=True：从当前工作目录开始向上找，而不是从本文件位置开始
    found = find_dotenv(usecwd=True)
    return Path(found) if found else None


def load_env_file(path: str | Path | None = None) -> Path | None:
    """把 .env 里的键值加载进 os.environ。

    支持的写法由 python-dotenv 负责，比手写解析宽松：
        KEY=value
        KEY="value"
        export KEY=value
        KEY=value  # 行尾注释
        多行值（用引号包裹）

    Args:
        path: 指定文件路径；为 None 时按当前目录 -> 父目录的顺序查找。

    Returns:
        实际加载的文件路径；没找到文件时返回 None。
    """
    target = _resolve_path(path)
    if target is None or not target.is_file():
        return None

    # encoding="utf-8-sig"：兼容 Windows 记事本保存时带的 BOM
    load_dotenv(dotenv_path=target, override=False, encoding="utf-8-sig")
    return target
