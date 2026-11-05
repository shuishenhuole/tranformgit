"""命令行入口：参数解析、客户端装配、批量调度、结果汇总。

退出码约定（方便挂到 CI 或 shell 判断）：
    0  全部成功
    1  有仓库迁移失败，或有致命错误（token 无效等）
    2  参数用法错误
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import load_env_file
from .gitee import GiteeClient
from .github import GithubClient
from .http import ApiError
from .migrate import MigrateOptions, RepoResult, migrate_repository, parse_source


def _setup_stdout() -> None:
    """Windows 控制台默认不是 UTF-8，中文和 emoji 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def log(message: str = "") -> None:
    """统一的输出口，flush 保证管道/重定向时也能实时看到进度。"""
    print(message, flush=True)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（migrate / list / check 三个子命令）。"""
    parser = argparse.ArgumentParser(
        prog="reposor-trans",
        description="把 Gitee 仓库迁移到 GitHub（代码 + 分支 + tag + Issue + Release）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python main.py migrate --gitee alice/demo\n"
            "  python main.py migrate --gitee alice/demo --github-owner my-org --visibility private\n"
            "  python main.py migrate --gitee-owner alice --all --skip-issues\n"
            "  python main.py list --gitee-owner alice\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        # 每个子命令都支持指定 .env，方便把配置放在别处
        sp.add_argument("--env-file", help="指定 .env 文件路径，默认读取当前目录 .env")

    # migrate ----------------------------------------------------------
    m = sub.add_parser("migrate", help="迁移一个或一批仓库")
    add_common(m)
    m.add_argument("--gitee", help="源仓库，格式 owner/repo")
    m.add_argument("--gitee-owner", help="源账号或组织，配合 --all 批量迁移")
    m.add_argument("--all", action="store_true", help="批量迁移 --gitee-owner 下的全部仓库")
    m.add_argument("--only", help="批量模式下只迁移这些仓库，逗号分隔")
    m.add_argument("--include-forks", action="store_true", help="批量模式下包含 fork 仓库")
    m.add_argument("--github-owner", help="目标 GitHub 账号/组织，默认当前登录用户")
    m.add_argument("--github-name", help="目标仓库名，默认与源仓库同名")
    m.add_argument(
        "--visibility",
        choices=["auto", "public", "private"],
        default="auto",
        help="目标仓库可见性，auto 表示与源仓库一致（默认）",
    )
    m.add_argument("--description", help="覆盖目标仓库简介")
    m.add_argument("--skip-git", action="store_true", help="不迁移代码，只建仓库/Issue")
    m.add_argument("--skip-issues", action="store_true", help="不迁移 Issue")
    m.add_argument("--skip-releases", action="store_true", help="不迁移 Release")
    m.add_argument("--with-assets", action="store_true", help="同时搬运 Release 附件（较慢）")
    m.add_argument("--no-annotate", action="store_true", help="Issue 正文不加来源说明头")
    m.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="不跳过目标仓库中已存在同名 Issue（默认会跳过，便于断点续传）",
    )
    m.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何写操作")

    # list -------------------------------------------------------------
    ls = sub.add_parser("list", help="列出 Gitee 账号/组织下的仓库")
    add_common(ls)
    ls.add_argument("--gitee-owner", required=True, help="Gitee 账号或组织")
    ls.add_argument("--include-forks", action="store_true", help="包含 fork 仓库")

    # check ------------------------------------------------------------
    check = sub.add_parser("check", help="校验 Gitee / GitHub token 是否可用")
    add_common(check)

    return parser


def _get_clients(*, need_github: bool) -> tuple[GiteeClient, GithubClient | None]:
    """从环境变量装配两个客户端。

    Gitee token 缺失只警告（公开仓库仍可读），GitHub token 缺失直接退出，
    因为没有它连仓库都建不了。
    """
    gitee_token = os.environ.get("GITEE_TOKEN", "").strip() or None
    github_token = os.environ.get("GITHUB_TOKEN", "").strip() or None

    if need_github and not github_token:
        raise SystemExit("缺少 GITHUB_TOKEN，请先设置环境变量或写入 .env")
    if not gitee_token:
        log("! 未设置 GITEE_TOKEN，只能迁移公开仓库（且 API 限流更严格）")

    return GiteeClient(gitee_token), (GithubClient(github_token) if github_token else None)


def _print_summary(results: list[RepoResult]) -> int:
    """打印结果表，返回进程退出码。"""
    failed = [r for r in results if not r.ok]
    log("\n" + "=" * 72)
    log("迁移结果")
    log("=" * 72)
    for r in results:
        if not r.ok:
            log(f"✗ {r.source} -> {r.target or '(未创建)'}")
            for err in r.errors:
                log(f"    错误: {err}")
        else:
            detail = (
                f"分支 {r.branches} / tag {r.tags} / Issue {r.issues} "
                f"(评论 {r.comments}) / Release {r.releases}"
            )
            if r.assets:
                detail += f" / 附件 {r.assets}"
            log(f"✓ {r.source} -> {r.repo_url}")
            log(f"    {detail}")
        # warning 在成功和失败两种情况下都要显示
        for warn in r.warnings:
            log(f"    ! {warn}")

    log("-" * 72)
    log(f"成功 {len(results) - len(failed)} 个，失败 {len(failed)} 个")
    return 1 if failed else 0


def _cmd_check(args: argparse.Namespace) -> int:
    """校验两个 token 是否可用。"""
    gitee_token = os.environ.get("GITEE_TOKEN", "").strip() or None
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    code = 0

    if gitee_token:
        try:
            # /user 是验证 token 最轻的接口
            user = GiteeClient(gitee_token).get("/user")
            log(f"✓ Gitee token 有效：{user.get('login')}")
        except ApiError as exc:
            log(f"✗ Gitee 校验失败：{exc}")
            code = 1
    else:
        log("· 未配置 GITEE_TOKEN（只能访问公开仓库，且批量列表接口可能 404）")

    if not github_token:
        log("✗ 缺少 GITHUB_TOKEN")
        return 1
    try:
        me = GithubClient(github_token).whoami()
        log(f"✓ GitHub token 有效：{me.get('login')}")
    except ApiError as exc:
        log(f"✗ GitHub 校验失败：{exc}")
        code = 1
    return code


def _cmd_list(args: argparse.Namespace) -> int:
    """列出源账号下的仓库，迁移前先看一眼有什么。"""
    gitee, _ = _get_clients(need_github=False)
    try:
        repos = gitee.list_repos(args.gitee_owner, include_forks=args.include_forks)
    except ApiError as exc:
        log(f"✗ 拉取仓库列表失败：{exc}")
        return 1

    if not repos:
        log("没有找到仓库")
        return 0
    for repo in repos:
        flag = "private" if repo.get("private") else "public"
        name = repo.get("full_name") or repo.get("path") or ""
        log(f"{name:<50} {flag:<8} {repo.get('default_branch') or ''}")
    log(f"\n共 {len(repos)} 个仓库")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    """迁移主流程：校验参数 -> 装配客户端 -> 逐个迁移 -> 汇总。"""
    # --- 参数校验：先把明显写错的挡在发请求之前 ---
    if not args.gitee and not (args.gitee_owner and args.all):
        log("✗ 请指定 --gitee owner/repo，或使用 --gitee-owner X --all 批量迁移")
        return 2
    if args.gitee and args.all:
        log("✗ --gitee 与 --all 不能同时使用")
        return 2
    if args.gitee:
        try:
            parse_source(args.gitee)
        except ValueError as exc:
            log(f"✗ {exc}")
            return 2

    # --- 客户端与身份 ---
    gitee, gh = _get_clients(need_github=True)
    assert gh is not None

    try:
        me = gh.whoami()
    except ApiError as exc:
        log(f"✗ 无法访问 GitHub，请检查 token：{exc}")
        return 1
    login = me["login"]
    log(f"GitHub 已登录：{login}")

    # CLI 参数 -> 迁移选项（skip_existing 与 allow_duplicates 是反义）
    opts = MigrateOptions(
        skip_git=args.skip_git,
        skip_issues=args.skip_issues,
        skip_releases=args.skip_releases,
        skip_existing=not args.allow_duplicates,
        with_assets=args.with_assets,
        annotate=not args.no_annotate,
        visibility=args.visibility,
        github_name=args.github_name,
        github_owner=args.github_owner,
        description=args.description,
        dry_run=args.dry_run,
    )

    # --- 确定要迁移哪些仓库 ---
    sources: list[str] = []
    if args.gitee:
        sources = [args.gitee]
    else:
        try:
            repos = gitee.list_repos(args.gitee_owner, include_forks=args.include_forks)
        except ApiError as exc:
            log(f"✗ 拉取仓库列表失败：{exc}")
            return 1
        # --only 用仓库名过滤，逗号分隔
        only = {s.strip() for s in (args.only or "").split(",") if s.strip()}
        for repo in repos:
            name = repo.get("path") or repo.get("name")
            if not name:
                continue
            if only and name not in only:
                continue
            # namespace 比参数更可靠（组织仓库的 owner 可能和传入值大小写不同）
            owner = (repo.get("namespace") or {}).get("path") or args.gitee_owner
            sources.append(f"{owner}/{name}")
        if not sources:
            log("没有匹配的仓库")
            return 0
        log(f"待迁移 {len(sources)} 个仓库：{', '.join(sources)}")

    # --- 逐个迁移，单个失败不影响其余 ---
    results: list[RepoResult] = []
    for source in sources:
        try:
            results.append(
                migrate_repository(
                    gitee,
                    gh,
                    source,
                    github_login=login,
                    opts=opts,
                    log=log,
                )
            )
        except KeyboardInterrupt:
            # Ctrl+C 立刻停手，已完成的仓库结果保留
            log("\n已中断")
            break
        except Exception as exc:  # noqa: BLE001 - 批量场景要兜住任何意外
            log(f"✗ {source} 异常终止：{exc}")
            results.append(RepoResult(source=source, errors=[str(exc)]))

    if not results:
        return 1
    return _print_summary(results)


def main(argv: list[str] | None = None) -> int:
    """CLI 总入口，返回退出码。"""
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    # 先加载 .env，再读环境变量，否则文件里的 token 用不上
    env_path = load_env_file(getattr(args, "env_file", None))
    if env_path:
        log(f"已加载环境变量: {env_path}")

    if args.command == "migrate":
        return _cmd_migrate(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "check":
        return _cmd_check(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
