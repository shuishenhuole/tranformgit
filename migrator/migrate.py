"""迁移流程编排：仓库 -> 代码 -> 默认分支 -> Issue -> Release。

顺序是刻意安排的：
1. 先建目标仓库，后面所有写操作才有地方落；
2. 再搬代码，因为 Release 依赖 tag 已存在；
3. 最后搬 Issue / Release，任何一步失败都不影响代码已经安全落地。

单个仓库的失败不会向上抛，而是记进 RepoResult.errors 由 CLI 汇总，
这样批量迁移时一个坏仓库不会中断整批。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .gitee import GiteeClient
from .git_ops import GitError, mirror_repository
from .github import GithubClient
from .http import ApiError

LogFn = Callable[[str], None]


@dataclass
class MigrateOptions:
    """一次迁移的开关集合，CLI 参数直接映射到这里。"""

    skip_git: bool = False
    skip_issues: bool = False
    skip_releases: bool = False
    # True 时按标题跳过目标仓库已有的 Issue，让重跑具备断点续传能力
    skip_existing: bool = True
    # 是否搬运 Release 附件（大文件很慢，默认关）
    with_assets: bool = False
    # 是否在 Issue/评论正文里加「迁移自 Gitee」的来源说明
    annotate: bool = True
    visibility: str = "auto"  # auto | public | private
    github_name: str | None = None  # 覆盖目标仓库名
    github_owner: str | None = None  # 目标账号/组织
    description: str | None = None  # 覆盖仓库简介
    dry_run: bool = False  # 只打印计划，不做写操作


@dataclass
class RepoResult:
    """单个仓库的迁移结果，用于最后打汇总表。"""

    source: str
    target: str = ""
    repo_url: str = ""
    branches: int = 0
    tags: int = 0
    issues: int = 0
    comments: int = 0
    releases: int = 0
    assets: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """只要有 error 就算失败；warning 不算（比如某个附件没传上去）。"""
        return not self.errors


# ------------------------------------------------------------------ 小工具
def parse_source(value: str) -> tuple[str, str]:
    """把各种写法的仓库地址归一化成 (owner, repo)。

    支持：
        owner/repo
        https://gitee.com/owner/repo
        https://gitee.com/owner/repo.git
        owner/repo/（尾部斜杠）
    """
    text = value.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("http"):
        text = text.split("://", 1)[1]
        if text.startswith("gitee.com/"):
            text = text[len("gitee.com/") :]
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"仓库格式应为 owner/repo，收到: {value!r}")
    # 取最后两段，兼容带上域名或前缀的写法
    return parts[-2], parts[-1]


def _normalize_color(color: str | None) -> str:
    """Gitee 的标签颜色转成 GitHub 要的 6 位十六进制。

    Gitee 常见 "#FF2E93" 或带透明度的 "3fb950aa"，GitHub 只接受 6 位且不带 #。
    """
    value = (color or "").strip().lstrip("#")
    value = "".join(ch for ch in value if ch.isalnum())
    if len(value) >= 6:
        return value[:6].lower()
    return "ededed"  # 兜底用 GitHub 的浅灰


def _fmt_time(value: str | None) -> str:
    """ISO 时间转成人类可读的 YYYY-MM-DD HH:MM，解析失败就原样返回。"""
    if not value:
        return "未知时间"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _is_closed(state: str | None) -> bool:
    """Gitee 的状态值比 GitHub 多，关闭类状态都归一成 closed。

    Gitee 可能返回：open / progressing / closed / rejected
    """
    return (state or "").lower() in {"closed", "rejected", "completed"}


def _issue_body(issue: dict[str, Any], annotate: bool) -> str:
    """拼 Issue 正文。

    GitHub API 不允许指定 created_at / 作者，所以原始信息只能以引用块的
    形式写在正文开头，方便日后溯源。annotate=False 时只保留正文。
    """
    body = (issue.get("body") or "").strip()
    if not annotate:
        return body
    user = (issue.get("user") or {}).get("login") or "unknown"
    meta = [f"原作者：@{user}", f"原状态：{issue.get('state') or 'open'}"]
    meta.append(f"原创建时间：{_fmt_time(issue.get('created_at'))}")
    if issue.get("html_url"):
        meta.append(f"原链接：{issue['html_url']}")
    header = "> 🔁 该 Issue 迁移自 Gitee ｜ " + " ｜ ".join(meta)
    return f"{header}\n\n{body}" if body else header


def _comment_body(comment: dict[str, Any]) -> str:
    """拼评论正文，同样保留原作者和时间。

    评论不能单独指定作者，所以把「谁说的」写进引用行里。
    """
    user = (comment.get("user") or {}).get("login") or "unknown"
    body = (comment.get("body") or "").strip()
    return f"> **@{user}** 于 {_fmt_time(comment.get('created_at'))} 评论（迁移自 Gitee）\n\n{body}"


# ------------------------------------------------------------------ 各环节
def _migrate_issues(
    gitee: GiteeClient,
    gh: GithubClient,
    src: tuple[str, str],
    dst: tuple[str, str],
    opts: MigrateOptions,
    result: RepoResult,
    log: LogFn,
) -> None:
    """搬 Issue：标签 -> 正文 -> 评论 -> 关闭状态。

    编号会变：Gitee 的 #12 到 GitHub 可能是 #3，所以每条都记下新编号，
    评论要挂到新编号上，不能沿用旧号。
    """
    g_owner, g_repo = src
    t_owner, t_repo = dst

    issues = gitee.list_issues(g_owner, g_repo)
    if not issues:
        log("  · 源仓库没有 Issue")
        return

    # 断点续传的基础：先取目标仓库已有 Issue 的标题集合
    existing_titles: set[str] = set()
    if opts.skip_existing:
        try:
            existing_titles = {i["title"] for i in gh.list_issues(t_owner, t_repo)}
        except ApiError as exc:
            # 读不到就当作没有，宁可重复也不要整批失败
            result.warnings.append(f"读取目标仓库 Issue 失败：{exc}")

    # 一次取全量标签，避免每条 Issue 都去查一遍
    existing_labels: set[str] = set()
    try:
        existing_labels = {l["name"].lower() for l in gh.list_labels(t_owner, t_repo)}
    except ApiError:
        pass  # 标签缺失不影响主流程

    log(f"  · 迁移 {len(issues)} 个 Issue")
    for issue in issues:
        title = (issue.get("title") or "").strip() or "untitled"
        number = issue.get("number")
        try:
            # 同名已存在 => 上次已经搬过，跳过（评论也不再重复）
            if opts.skip_existing and title in existing_titles:
                log(f"    - #{number} 已存在，跳过：{title[:40]}")
                continue

            # 先补齐标签，否则建 Issue 时引用的标签会被 GitHub 自建并丢掉颜色
            labels: list[str] = []
            for label in issue.get("labels") or []:
                name = (label.get("name") or "").strip()
                if not name:
                    continue
                key = name.lower()[:50]
                if key not in existing_labels:
                    gh.ensure_label(
                        t_owner,
                        t_repo,
                        key,
                        _normalize_color(label.get("color")),
                        label.get("description") or "",
                    )
                    existing_labels.add(key)
                labels.append(key)

            created = gh.create_issue(
                t_owner,
                t_repo,
                title=title,
                body=_issue_body(issue, opts.annotate),
                labels=labels or None,
            )
            new_number = created["number"]
            result.issues += 1
            existing_titles.add(title)

            # 评论按时间顺序逐条追加，保持原对话顺序
            for comment in gitee.list_issue_comments(g_owner, g_repo, number):
                body = _comment_body(comment)
                if not body.strip():
                    continue
                gh.create_comment(t_owner, t_repo, new_number, body)
                result.comments += 1

            # 关闭状态必须最后设置：GitHub 不允许创建时直接指定 closed
            if _is_closed(issue.get("state")):
                gh.close_issue(t_owner, t_repo, new_number)
        except ApiError as exc:
            # 单条 Issue 失败不影响其余 Issue
            message = f"Issue #{number} 迁移失败：{exc}"
            result.errors.append(message)
            log(f"    ✗ {message}")


def _migrate_releases(
    gitee: GiteeClient,
    gh: GithubClient,
    src: tuple[str, str],
    dst: tuple[str, str],
    opts: MigrateOptions,
    result: RepoResult,
    log: LogFn,
) -> None:
    """搬 Release：tag 关联的元信息，附件可选。

    tag 由代码迁移阶段推上去，这里只建 Release 记录本身。
    """
    g_owner, g_repo = src
    t_owner, t_repo = dst

    try:
        releases = gitee.list_releases(g_owner, g_repo)
    except ApiError as exc:
        result.warnings.append(f"读取 Release 失败：{exc}")
        return
    if not releases:
        log("  · 源仓库没有 Release")
        return

    log(f"  · 迁移 {len(releases)} 个 Release")
    for rel in releases:
        tag = rel.get("tag_name")
        if not tag:
            continue
        try:
            created = gh.create_release(
                t_owner,
                t_repo,
                tag_name=tag,
                title=rel.get("name") or tag,
                body=rel.get("body") or "",
                prerelease=bool(rel.get("prerelease")),
            )
            result.releases += 1
        except ApiError as exc:
            # 422 一般是 tag 不存在或 Release 已存在，属于可跳过的情况
            if exc.status == 422:
                result.warnings.append(f"Release {tag} 已存在或 tag 缺失，跳过")
                log(f"    - Release {tag} 已存在或 tag 缺失，跳过")
                continue
            result.errors.append(f"Release {tag} 迁移失败：{exc}")
            log(f"    ✗ Release {tag} 失败：{exc}")
            continue

        assets = rel.get("assets") or []
        if not (opts.with_assets and assets):
            continue
        # 附件先下到临时目录再上传，避免大文件常驻内存
        with tempfile.TemporaryDirectory(prefix="reposor-asset-") as tmp:
            for asset in assets:
                url = asset.get("browser_download_url")
                name = asset.get("name") or "asset"
                if not url:
                    continue
                try:
                    log(f"    · 下载 asset {name}")
                    local = Path(tmp) / name
                    gitee.download(url, local)
                    gh.upload_release_asset(t_owner, t_repo, created["id"], local, name)
                    result.assets += 1
                except (ApiError, OSError) as exc:
                    # 附件失败只记 warning：Release 主体已经建好了
                    result.warnings.append(f"asset {name} 迁移失败：{exc}")
                    log(f"    ! asset {name} 失败：{exc}")


# ------------------------------------------------------------------ 入口
def migrate_repository(
    gitee: GiteeClient,
    gh: GithubClient,
    source: str,
    *,
    github_login: str,
    opts: MigrateOptions,
    log: LogFn = print,
) -> RepoResult:
    """迁移单个仓库，返回结果对象（不抛异常）。

    Args:
        source: "owner/repo" 或完整 Gitee 地址。
        github_login: 当前 GitHub 登录名，用于判断目标是用户还是组织。
        opts: 迁移开关。
        log: 日志输出函数。
    """
    result = RepoResult(source=source)
    try:
        src_owner, src_repo = parse_source(source)
    except ValueError as exc:
        result.errors.append(str(exc))
        return result

    # 源仓库元信息是后续所有决策的依据；取不到就直接结束
    info = gitee.get_repo(src_owner, src_repo)
    if not info:
        result.errors.append(f"Gitee 仓库不存在或无权访问：{source}")
        return result

    name = opts.github_name or info.get("path") or info.get("name") or src_repo
    target_owner = opts.github_owner or github_login
    result.target = f"{target_owner}/{name}"
    result.repo_url = f"https://github.com/{target_owner}/{name}"

    # auto 表示跟随源仓库；显式指定时以参数为准
    private = (
        bool(info.get("private")) if opts.visibility == "auto" else opts.visibility == "private"
    )
    description = opts.description if opts.description is not None else (info.get("description") or "")
    homepage = info.get("homepage") or ""

    log(f"\n▶ {src_owner}/{src_repo}  ->  {result.target}  ({'private' if private else 'public'})")

    # 1) 目标仓库：已存在就复用，保证脚本可重跑
    try:
        existing = gh.get_repo(target_owner, name)
        if existing:
            log("  · 目标仓库已存在，复用")
        elif opts.dry_run:
            log("  · [dry-run] 将创建目标仓库")
        else:
            gh.create_repo(
                name,
                private=private,
                description=description,
                homepage=homepage,
                # 只有目标不是当前登录用户时才走组织接口
                org=target_owner if target_owner.lower() != github_login.lower() else None,
            )
            log("  ✓ 已创建目标仓库")
    except ApiError as exc:
        result.errors.append(f"创建目标仓库失败：{exc}")
        return result

    # 2) 代码：分支 + tag 全量搬迁
    if opts.skip_git:
        log("  · 跳过代码迁移")
    elif opts.dry_run:
        log("  · [dry-run] 将镜像推送所有分支与 tag")
    else:
        try:
            stats = mirror_repository(
                f"https://gitee.com/{src_owner}/{src_repo}.git",
                f"https://github.com/{target_owner}/{name}.git",
                # Gitee 用仓库 owner 当用户名，token 当密码
                source_username=src_owner,
                source_token=gitee.token,
                # GitHub 用 x-access-token + PAT，是官方推荐的 https 认证方式
                target_username=github_login,
                target_token=gh.token,
                log=log,
            )
            result.branches = stats["branches"]
            result.tags = stats["tags"]
        except GitError as exc:
            result.errors.append(f"代码迁移失败：{exc}")
            log(f"  ✗ 代码迁移失败：{exc}")
            # 代码没推上去，后面 Issue 里的引用会全部错位，没必要继续
            return result

    # 3) 默认分支对齐（Gitee 的 master 到 GitHub 还是 master，别被改成 main）
    default_branch = info.get("default_branch")
    if default_branch and not opts.skip_git and not opts.dry_run:
        try:
            gh.update_repo(target_owner, name, default_branch=default_branch)
            log(f"  · 默认分支设为 {default_branch}")
        except ApiError as exc:
            # 分支名对不上不影响代码本身，记 warning 即可
            result.warnings.append(f"设置默认分支失败：{exc}")

    # 4) Issue 与 Release
    if opts.skip_issues:
        log("  · 跳过 Issue 迁移")
    elif opts.dry_run:
        log("  · [dry-run] 将迁移 Issue / Release")
    else:
        _migrate_issues(gitee, gh, (src_owner, src_repo), (target_owner, name), opts, result, log)

        # 5) Release 依赖 tag 已经推上去，所以放在代码迁移之后
        if opts.skip_releases:
            log("  · 跳过 Release 迁移")
        else:
            _migrate_releases(
                gitee, gh, (src_owner, src_repo), (target_owner, name), opts, result, log
            )

    return result
