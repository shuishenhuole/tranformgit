"""sources_from_json（--from-json 清单解析）与 CLI 参数互斥校验。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from migrator.migrate import sources_from_json

ROOT = Path(__file__).resolve().parent.parent


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "repos.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_reads_list_json_output(tmp_path):
    """`list --json` 落盘的标准格式能整份读出来。"""
    path = _write(
        tmp_path,
        [
            {
                "full_name": "shuishenhuole2024/obsidian",
                "private": True,
                "fork": False,
                "default_branch": "master",
                "html_url": "https://gitee.com/shuishenhuole2024/obsidian.git",
                "description": "",
            },
            {"full_name": "shuishenhuole2024/tranformgit", "fork": False},
        ],
    )
    assert sources_from_json(path) == [
        "shuishenhuole2024/obsidian",
        "shuishenhuole2024/tranformgit",
    ]


def test_skips_forks_and_bad_entries_by_default(tmp_path):
    """fork 默认排除（与 --all 行为一致）；缺 full_name / 非对象的条目跳过。"""
    path = _write(
        tmp_path,
        [
            {"full_name": "a/real", "fork": False},
            {"full_name": "a/forked", "fork": True},
            {"description": "缺 full_name 的坏条目"},
            "不是对象的条目",
        ],
    )
    assert sources_from_json(path) == ["a/real"]
    assert sources_from_json(path, include_forks=True) == ["a/real", "a/forked"]


def test_filters_by_owner(tmp_path):
    """--owner 只保留该账号/组织下的仓库。"""
    path = _write(
        tmp_path,
        [
            {"full_name": "alice/demo"},
            {"full_name": "bob/other"},
            {"full_name": "alice/another"},
        ],
    )
    assert sources_from_json(path, owner="alice") == ["alice/demo", "alice/another"]


def test_dedupes_repeated_entries(tmp_path):
    """同一仓库出现多次只迁一次。"""
    path = _write(tmp_path, [{"full_name": "a/demo"}, {"full_name": "a/demo"}])
    assert sources_from_json(path) == ["a/demo"]


def test_rejects_bad_files(tmp_path):
    """文件不存在 / 内容不是 JSON / 顶层不是数组都要报错。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        sources_from_json(bad)

    not_a_list = _write(tmp_path, {"full_name": "a/demo"})
    with pytest.raises(ValueError):
        sources_from_json(not_a_list)

    with pytest.raises(ValueError):
        sources_from_json(tmp_path / "missing.json")


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_rejects_repo_with_from_json():
    proc = _run_cli("migrate", "a/b", "--from-json", "gitee.json")
    assert proc.returncode == 2
    assert "不能同时使用" in proc.stderr


def test_cli_rejects_all_with_from_json():
    proc = _run_cli("migrate", "--all", "--from-json", "gitee.json")
    assert proc.returncode == 2
    assert "不能同时使用" in proc.stderr


def test_cli_missing_json_file_fails_before_network():
    """清单文件读不到要立刻报错，且退出码是 2（参数/输入错误）。"""
    proc = _run_cli("migrate", "--from-json", str(ROOT / "no-such-file.json"))
    assert proc.returncode == 2
    assert "读取仓库清单失败" in proc.stderr
