"""极简 .env 读取，避免为了读两行配置引入 python-dotenv 依赖。"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path | None = None) -> Path | None:
    """把 .env 里的键值写入 os.environ，已存在的环境变量不覆盖。

    不覆盖是刻意的：命令行里 `GITHUB_TOKEN=xxx python main.py ...` 这种临时
    指定应该优先于文件里的值。

    支持的写法：
        KEY=value
        KEY="value"
        export KEY=value
        # 注释行

    Args:
        path: 指定文件路径；为 None 时读取当前工作目录下的 .env。

    Returns:
        实际加载的文件路径；没找到任何文件时返回 None。
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path).expanduser())
    else:
        candidates.append(Path.cwd() / ".env")

    for file_path in candidates:
        if not file_path.is_file():
            continue
        # utf-8-sig：兼容 Windows 记事本保存时带的 BOM
        for raw in file_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            # 去掉成对的引号，但不动值内部的内容（token 里可能有引号字符）
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return file_path
    return None
