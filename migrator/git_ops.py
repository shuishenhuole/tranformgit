"""git 命令封装：镜像克隆源仓库，再把全部引用推到目标仓库。

为什么不直接 `git push --mirror`：
    `--mirror` 会把源仓库所有 ref 照搬，包括 Gitee 内部的 refs/pull/*，
    而 GitHub 明确拒收 refs/pull/*，整条 push 会失败。
    所以这里改成显式指定两条 refspec：heads 和 tags，只搬真正需要的。

安全约定：
- 凭证只临时拼进 URL，不写进任何 remote 配置，进程结束就没了
- 所有对外输出都过一遍 mask()，防止 token 出现在日志/报错里
- 设置 GIT_TERMINAL_PROMPT=0，凭证错误时直接失败，不会卡在密码提示
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


class GitError(RuntimeError):
    """git 命令执行失败（已做 token 脱敏）。"""


def _run(args: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    """跑一条 git 命令，返回 (退出码, 合并后的 stdout+stderr)。

    不抛异常，由调用方决定怎么处理，方便把输出统一过 mask()。
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # 禁止交互式输入密码
    env["GCM_INTERACTIVE"] = "never"  # 禁止 Git Credential Manager 弹窗
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # Windows 上 git 输出可能混入 GBK，别让解码炸掉
        env=env,
    )
    output = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
    return proc.returncode, output


def ensure_git() -> str:
    """确认 git 可用，返回版本号；不可用时抛 GitError。"""
    try:
        code, out = _run(["git", "--version"])
    except FileNotFoundError as exc:
        raise GitError("未找到 git 命令，请先安装 Git") from exc
    if code != 0:
        raise GitError(f"git 不可用: {out}")
    return out


def has_lfs() -> bool:
    """本机是否装了 git-lfs（决定能不能搬运 LFS 实体对象）。"""
    try:
        code, _ = _run(["git", "lfs", "version"])
    except FileNotFoundError:
        return False
    return code == 0


def with_credentials(url: str, username: str, token: str | None) -> str:
    """把 user:token 注入 https URL。

    只在内存里临时用，push 完这个字符串就丢了，不会落到 .git/config。
    token 里可能出现特殊字符，所以两边都做 URL 编码。
    """
    if not token:
        return url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return url  # 本地路径或 ssh 地址不需要注入
    netloc = f"{quote(username, safe='')}:{quote(token, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def mask(text: str, secrets: list[str]) -> str:
    """把文本里出现的 token 替换成 ***，用于打日志和抛错。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _count_refs(mirror_dir: Path) -> tuple[int, int]:
    """统计镜像里的分支数和 tag 数，用于进度提示和结果汇总。

    裸仓库不能用 `git branch`，只能用 for-each-ref。
    """
    code, out = _run(
        ["git", "-C", str(mirror_dir), "for-each-ref", "--format=%(refname)"]
    )
    if code != 0:
        return 0, 0
    branches = sum(1 for line in out.splitlines() if line.startswith("refs/heads/"))
    tags = sum(1 for line in out.splitlines() if line.startswith("refs/tags/"))
    return branches, tags


def _uses_lfs(mirror_dir: Path) -> bool:
    """判断仓库是否用了 Git LFS：看 .gitattributes 里有没有 filter=lfs。"""
    candidates = (mirror_dir / ".gitattributes", mirror_dir / "info" / "attributes")
    for candidate in candidates:
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "filter=lfs" in content:
                return True
    return False


def mirror_repository(
    source_url: str,
    target_url: str,
    *,
    source_username: str | None,
    source_token: str | None,
    target_username: str | None,
    target_token: str | None,
    workdir: str | Path | None = None,
    log=print,
) -> dict[str, int]:
    """镜像克隆源仓库并强推到目标仓库。

    Args:
        source_url: 源仓库 https 地址（不带凭证）。
        target_url: 目标仓库 https 地址（不带凭证）。
        source_username: 源站用户名，Gitee 用仓库 owner 即可。
        target_username: 目标站用户名，GitHub 用当前登录账号。
        workdir: 指定镜像存放目录；None 则用临时目录，结束后自动删除。
        log: 日志函数，便于 CLI 统一输出格式。

    Returns:
        {"branches": 分支数, "tags": tag 数}

    Raises:
        GitError: 克隆或推送失败（消息里的 token 已脱敏）。
    """
    src = with_credentials(source_url, source_username or "oauth2", source_token)
    dst = with_credentials(
        target_url, target_username or "x-access-token", target_token
    )
    secrets = [s for s in (source_token, target_token) if s]

    # workdir 为空说明是临时目录，函数退出时要负责清理
    created_tmp = workdir is None
    root = (
        Path(tempfile.mkdtemp(prefix="reposor-mirror-"))
        if created_tmp
        else Path(workdir).expanduser().resolve()
    )
    root.mkdir(parents=True, exist_ok=True)
    mirror_dir = root / "mirror.git"

    try:
        # 指定 workdir 时目录可能已有上次的镜像，先清掉避免引用混乱
        if mirror_dir.exists():
            shutil.rmtree(mirror_dir, ignore_errors=True)

        log(f"  · 镜像克隆 {source_url}")
        code, out = _run(["git", "clone", "--mirror", src, str(mirror_dir)])
        if code != 0:
            raise GitError(mask(f"clone 失败:\n{out}", secrets))

        branches, tags = _count_refs(mirror_dir)
        log(f"  · 本地镜像: {branches} 个分支 / {tags} 个 tag")

        # 空仓库（只有 README 也算有分支，这里特指连分支都没有）
        if branches == 0:
            log("  ! 源仓库没有任何分支，跳过 push")
            return {"branches": 0, "tags": tags}

        # LFS：mirror clone 只拿到指针文件，得显式 fetch 一遍才会从源站拉实体
        if _uses_lfs(mirror_dir):
            if has_lfs():
                log("  · 检测到 Git LFS，拉取全部 LFS 对象（可能较慢）")
                code, out = _run(
                    ["git", "-C", str(mirror_dir), "lfs", "fetch", "--all"]
                )
                if code != 0:
                    # LFS 失败不致命，至少代码历史还在
                    log(f"  ! LFS 拉取失败，继续推送指针: {mask(out[:300], secrets)}")
            else:
                log("  ! 源仓库使用了 Git LFS，但本机没有 git-lfs，只会推送指针文件")

        log(f"  · 推送全部分支与 tag -> {target_url}")
        code, out = _run(
            [
                "git",
                "-C",
                str(mirror_dir),
                "push",
                "--force",  # 目标仓库若已存在则覆盖，保证重跑幂等
                "--prune",  # 源仓库删掉的分支，目标端也一并删除
                dst,
                "refs/heads/*:refs/heads/*",
                "refs/tags/*:refs/tags/*",
            ]
        )
        if code != 0:
            raise GitError(mask(f"push 失败:\n{out}", secrets))

        return {"branches": branches, "tags": tags}
    finally:
        # 只用临时目录时才清理，用户指定的 workdir 保留下来方便排查
        if created_tmp:
            shutil.rmtree(root, ignore_errors=True)


def remote_default_branch(
    url: str, token: str | None, username: str = "x-access-token"
) -> str | None:
    """用 ls-remote 探出远端默认分支（备用的轻量方案）。

    主流程用的是 Gitee API 返回的 default_branch，这个函数留给
    「只有 git 凭证、没有 API 权限」的场景。
    """
    code, out = _run(
        ["git", "ls-remote", "--symref", with_credentials(url, username, token), "HEAD"]
    )
    if code != 0:
        return None
    for line in out.splitlines():
        # 形如: ref: refs/heads/main	HEAD
        if line.startswith("ref:") and "HEAD" in line:
            return line.split("refs/heads/")[-1].split()[0]
    return None
