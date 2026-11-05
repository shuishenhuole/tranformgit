"""命令行入口（Typer 实现）。

三个子命令：
    check    校验两个 token 是否可用
    list     列出仓库（不带 --owner 时列出 token 能访问的全部仓库）
    migrate  迁移仓库（单个、指定账号、或一键全量）

退出码约定（方便挂到 CI 或 shell 判断）：
    0    全部成功
    1    有仓库迁移失败，或有致命错误（token 无效等）
    2    参数用法错误
    130  用户 Ctrl+C 中断
"""

from __future__ import annotations

import json
import os
import sys
from enum import Enum
from typing import Annotated

import typer

from . import ui
from .config import load_env_file
from .gitee import GiteeClient
from .github import GithubClient
from .http import ApiError
from .migrate import (
    MigrateOptions,
    RepoResult,
    migrate_repository,
    parse_source,
    repo_full_name,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="把 Gitee 仓库迁移到 GitHub（代码 + 分支 + tag + Issue + Release）",
)


class Visibility(str, Enum):
    """目标仓库可见性；auto 表示跟随源仓库。"""

    auto = "auto"
    public = "public"
    private = "private"


# 多个子命令共用的选项，抽成类型别名减少重复
EnvFileOpt = Annotated[
    str | None, typer.Option("--env-file", help="指定 .env 文件路径，默认读取当前目录 .env")
]

# 子命令里也放一份 --no-spinner：习惯把选项写在命令后面的人不会踩坑
SpinnerOpt = Annotated[
    bool, typer.Option("--no-spinner", help="关闭等待动画（重定向/CI 环境会自动关闭）")
]


@app.callback()
def _global_options(no_spinner: SpinnerOpt = False) -> None:
    """把 Gitee 仓库迁移到 GitHub。"""
    # 放在 callback 里，`main.py --no-spinner migrate ...` 这种写法也能用；
    # 子命令里还各有一份同名选项，覆盖 `main.py migrate ... --no-spinner`。
    if no_spinner:
        ui.configure(False)


def _setup_stdout() -> None:
    """Windows 控制台默认不是 UTF-8，中文和 emoji 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _load_env(env_file: str | None) -> None:
    """加载 .env 并提示结果。

    显式指定路径却没找到时给个警告：否则用户拼错路径会以为 token 没生效，
    排查半天才发现是文件名的问题。
    """
    loaded = load_env_file(env_file)
    if loaded:
        # 走 stderr：这是诊断信息，不能混进 list --json 的 stdout
        typer.echo(f"已加载环境变量: {loaded}", err=True)
    elif env_file:
        typer.secho(f"! 指定的 .env 不存在: {env_file}", fg=typer.colors.YELLOW, err=True)


def _maybe_disable_spinner(flag: bool) -> None:
    """子命令里的 --no-spinner。

    只负责「关」，不负责「开」：默认值交给 isatty 自动判断，
    这样重定向到文件或 CI 里运行时不会硬塞动画。
    """
    if flag:
        ui.configure(False)


def _fail(message: str, code: int = 1) -> None:
    """打印红色错误并以指定退出码结束。

    先收掉动画行：否则错误文字会和转圈挤在同一行，看起来像输出错乱。
    """
    ui.stop()
    typer.secho(f"✗ {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _get_clients(*, need_github: bool) -> tuple[GiteeClient, GithubClient | None]:
    """从环境变量装配两个客户端。

    Gitee token 缺失只警告（公开仓库仍可读），GitHub token 缺失直接退出，
    因为没有它连仓库都建不了。
    """
    gitee_token = os.environ.get("GITEE_TOKEN", "").strip() or None
    github_token = os.environ.get("GITHUB_TOKEN", "").strip() or None

    if need_github and not github_token:
        _fail("缺少 GITHUB_TOKEN，请先设置环境变量或写入 .env")
    if not gitee_token:
        typer.secho(
            "! 未设置 GITEE_TOKEN，只能迁移公开仓库（且批量列表接口会 404）",
            fg=typer.colors.YELLOW,
            err=True,
        )

    return GiteeClient(gitee_token), (GithubClient(github_token) if github_token else None)


# ------------------------------------------------------------------ check
@app.command()
def check(env_file: EnvFileOpt = None, no_spinner: SpinnerOpt = False) -> None:
    """校验 Gitee / GitHub token 是否可用。"""
    _load_env(env_file)
    _maybe_disable_spinner(no_spinner)
    gitee_token = os.environ.get("GITEE_TOKEN", "").strip() or None
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    code = 0

    if gitee_token:
        ui.start("  · 校验 Gitee token")
        try:
            # /user 是验证 token 最轻的接口
            user = GiteeClient(gitee_token).whoami()
            ui.stop(f"✓ Gitee token 有效：{user.get('login')}", fg=typer.colors.GREEN)
        except ApiError as exc:
            ui.stop(f"✗ Gitee 校验失败：{exc}", fg=typer.colors.RED)
            code = 1
    else:
        typer.secho(
            "· 未配置 GITEE_TOKEN（只能访问公开仓库，且无法列出仓库清单）",
            fg=typer.colors.YELLOW,
        )

    if not github_token:
        typer.secho("✗ 缺少 GITHUB_TOKEN", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    ui.start("  · 校验 GitHub token")
    try:
        me = GithubClient(github_token).whoami()
        ui.stop(f"✓ GitHub token 有效：{me.get('login')}", fg=typer.colors.GREEN)
    except ApiError as exc:
        ui.stop(f"✗ GitHub 校验失败：{exc}", fg=typer.colors.RED)
        code = 1
    raise typer.Exit(code=code)


# ------------------------------------------------------------------- list
@app.command("list")
def list_cmd(
    owner: Annotated[
        str | None,
        typer.Option("--owner", "-o", help="只看该账号/组织；不填则列出 token 能访问的全部仓库"),
    ] = None,
    forks: Annotated[bool, typer.Option("--forks", help="包含 fork 仓库")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON，便于脚本处理")] = False,
    env_file: EnvFileOpt = None,
    no_spinner: SpinnerOpt = False,
) -> None:
    """列出仓库（默认一键列出当前账号的全部仓库，含所属组织）。"""
    _load_env(env_file)
    _maybe_disable_spinner(no_spinner)
    gitee_token = os.environ.get("GITEE_TOKEN", "").strip() or None

    if not gitee_token and not owner:
        _fail("列出全部仓库需要 GITEE_TOKEN，请先配置（或改用 --owner 指定公开账号）", code=2)

    # JSON 结果要能直接喂给 jq：动画关掉，过程行也全部静音
    if as_json:
        ui.configure(False)
        ui.silence()
        progress_log = lambda _msg: None  # noqa: E731
    else:
        progress_log = ui.log

    gitee = GiteeClient(gitee_token)
    ui.start(f"  · 拉取 {owner} 的仓库列表" if owner else "  · 拉取全部仓库（个人 + 所属组织）")
    try:
        if owner:
            repos = gitee.list_repos(owner, include_forks=forks)
        else:
            # 没有 owner：个人仓库 + 所属组织仓库，一次拿全
            repos = gitee.list_all_repos(include_forks=forks, log=progress_log)
    except ApiError as exc:
        _fail(f"拉取仓库列表失败：{exc}")
        return
    ui.stop(f"  · 共 {len(repos)} 个仓库")

    if not repos:
        # json 模式下也要输出合法 JSON（空数组），否则脚本解析会炸
        typer.echo("[]" if as_json else "没有找到仓库")
        raise typer.Exit(code=0)

    if as_json:
        # 只保留有用字段，避免把整个 API 响应吐出来
        payload = [
            {
                "full_name": repo_full_name(r),
                "private": bool(r.get("private")),
                "fork": bool(r.get("fork")),
                "default_branch": r.get("default_branch"),
                "html_url": r.get("html_url"),
                "description": r.get("description"),
            }
            for r in repos
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        raise typer.Exit(code=0)

    for item in repos:
        name = repo_full_name(item) or ""
        flag = "private" if item.get("private") else "public"
        fork = " [fork]" if item.get("fork") else ""
        typer.echo(f"{name:<50} {flag:<8} {item.get('default_branch') or ''}{fork}")
    typer.echo(f"\n共 {len(repos)} 个仓库")


# ---------------------------------------------------------------- migrate
@app.command()
def migrate(
    repo: Annotated[
        str | None, typer.Argument(help="源仓库 owner/repo；使用 --all 时省略")
    ] = None,
    all_repos: Annotated[
        bool,
        typer.Option("--all", "-a", help="一键迁移当前 Gitee 账号的全部仓库（含所属组织）"),
    ] = False,
    owner: Annotated[
        str | None, typer.Option("--owner", "-o", help="只迁移该账号/组织下的仓库")
    ] = None,
    only: Annotated[
        str | None, typer.Option("--only", help="只迁移这些仓库名，逗号分隔")
    ] = None,
    forks: Annotated[bool, typer.Option("--forks", help="批量时包含 fork 仓库")] = False,
    github_owner: Annotated[
        str | None, typer.Option("--github-owner", help="目标 GitHub 账号/组织，默认当前登录用户")
    ] = None,
    github_name: Annotated[
        str | None, typer.Option("--github-name", help="目标仓库名，默认与源仓库同名")
    ] = None,
    visibility: Annotated[
        Visibility, typer.Option("--visibility", help="目标仓库可见性，auto 表示跟随源仓库")
    ] = Visibility.auto,
    description: Annotated[
        str | None, typer.Option("--description", help="覆盖目标仓库简介")
    ] = None,
    skip_git: Annotated[bool, typer.Option("--skip-git", help="不迁移代码")] = False,
    skip_issues: Annotated[bool, typer.Option("--skip-issues", help="不迁移 Issue")] = False,
    skip_releases: Annotated[
        bool, typer.Option("--skip-releases", help="不迁移 Release")
    ] = False,
    with_assets: Annotated[
        bool, typer.Option("--with-assets", help="同时搬运 Release 附件（较慢）")
    ] = False,
    annotate: Annotated[
        bool,
        typer.Option("--annotate/--no-annotate", help="Issue 正文是否加来源说明头"),
    ] = True,
    allow_duplicates: Annotated[
        bool,
        typer.Option("--allow-duplicates", help="不跳过目标仓库已存在的同名 Issue"),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只打印计划，不做任何写操作")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="批量迁移时跳过确认提示")
    ] = False,
    env_file: EnvFileOpt = None,
    no_spinner: SpinnerOpt = False,
) -> None:
    """迁移仓库：单个、指定账号，或 --all 一键全量。"""
    _load_env(env_file)
    _maybe_disable_spinner(no_spinner)

    # --- 参数校验：先把明显写错的挡在发请求之前 ---
    if repo and all_repos:
        _fail("REPO 参数与 --all 不能同时使用", code=2)
    if not repo and not all_repos:
        _fail("请指定 REPO（如 alice/demo），或使用 --all 一键迁移", code=2)
    if repo:
        try:
            parse_source(repo)
        except ValueError as exc:
            _fail(str(exc), code=2)
        if owner:
            _fail("指定单个 REPO 时不需要 --owner", code=2)

    # --- 客户端与身份 ---
    gitee, gh = _get_clients(need_github=True)
    assert gh is not None

    ui.start("  · 校验 GitHub token")
    try:
        me = gh.whoami()
    except ApiError as exc:
        _fail(f"无法访问 GitHub，请检查 token：{exc}")
        return
    login = me["login"]
    ui.stop(f"GitHub 已登录：{login}")

    # CLI 参数 -> 迁移选项
    opts = MigrateOptions(
        skip_git=skip_git,
        skip_issues=skip_issues,
        skip_releases=skip_releases,
        skip_existing=not allow_duplicates,
        with_assets=with_assets,
        annotate=annotate,
        visibility=visibility.value,
        github_name=github_name,
        github_owner=github_owner,
        description=description,
        dry_run=dry_run,
    )

    # --- 确定要迁移哪些仓库 ---
    sources: list[str] = []
    if repo:
        sources = [repo]
    else:
        ui.start("  · 拉取全部仓库（个人 + 所属组织）")
        try:
            if owner:
                repos = gitee.list_repos(owner, include_forks=forks)
            else:
                # 一键：当前 token 能访问的全部仓库（个人 + 组织）
                repos = gitee.list_all_repos(include_forks=forks, log=ui.log)
        except ApiError as exc:
            _fail(f"拉取仓库列表失败：{exc}")
            return
        ui.stop()

        only_names = {s.strip() for s in (only or "").split(",") if s.strip()}
        for item in repos:
            full_name = repo_full_name(item)
            if not full_name:
                continue
            name = full_name.split("/")[-1]
            if only_names and name not in only_names:
                continue
            sources.append(full_name)

        if not sources:
            typer.echo("没有匹配的仓库")
            raise typer.Exit(code=0)
        typer.echo(f"待迁移 {len(sources)} 个仓库：{', '.join(sources)}")

        # 批量操作影响面大，非 --yes 时确认一次；非交互环境（管道/CI）直接跳过
        if len(sources) > 1 and not yes and sys.stdin.isatty():
            if not typer.confirm(f"确认迁移这 {len(sources)} 个仓库？"):
                typer.echo("已取消")
                raise typer.Exit(code=0)

    # --- 逐个迁移，单个失败不影响其余 ---
    results: list[RepoResult] = []
    total = len(sources)
    for index, source in enumerate(sources, 1):
        # 多仓库时带序号，一眼看出还剩几个
        tag = f"[{index}/{total}] " if total > 1 else ""
        ui.start(f"  · {tag}迁移 {source}")
        try:
            results.append(
                migrate_repository(
                    gitee,
                    gh,
                    source,
                    github_login=login,
                    opts=opts,
                    log=ui.log,
                )
            )
        except KeyboardInterrupt:
            # Ctrl+C 立刻停手，已完成的仓库结果保留
            ui.stop("! 已中断", fg=typer.colors.YELLOW)
            break
        except Exception as exc:  # noqa: BLE001 - 批量场景要兜住任何意外
            ui.stop(f"✗ {source} 异常终止：{exc}", fg=typer.colors.RED)
            results.append(RepoResult(source=source, errors=[str(exc)]))
        else:
            # 最后一次 log 还挂在动画行上，收线即把它定格成"这步耗时"
            ui.stop()

    if not results:
        raise typer.Exit(code=1)
    raise typer.Exit(code=_print_summary(results))


def _print_summary(results: list[RepoResult]) -> int:
    """打印结果表，返回进程退出码。"""
    failed = [r for r in results if not r.ok]
    typer.echo("\n" + "=" * 72)
    typer.echo("迁移结果")
    typer.echo("=" * 72)
    for r in results:
        if not r.ok:
            typer.secho(f"✗ {r.source} -> {r.target or '(未创建)'}", fg=typer.colors.RED)
            for err in r.errors:
                typer.echo(f"    错误: {err}")
        else:
            detail = (
                f"分支 {r.branches} / tag {r.tags} / Issue {r.issues} "
                f"(评论 {r.comments}) / Release {r.releases}"
            )
            if r.assets:
                detail += f" / 附件 {r.assets}"
            typer.secho(f"✓ {r.source} -> {r.repo_url}", fg=typer.colors.GREEN)
            typer.echo(f"    {detail}")
        # warning 在成功和失败两种情况下都要显示
        for warn in r.warnings:
            typer.secho(f"    ! {warn}", fg=typer.colors.YELLOW)

    typer.echo("-" * 72)
    typer.echo(f"成功 {len(results) - len(failed)} 个，失败 {len(failed)} 个")
    return 1 if failed else 0


# ------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    """CLI 总入口，返回退出码。

    这里让 Typer 保持 standalone_mode（默认值），再把它的 SystemExit 转成
    返回值。好处是不用关心 Typer 内部用的是哪个 click（0.2x 起 typer 自带
    一份 vendored click，直接捕获 click.ClickException 是抓不到的），
    参数错误、--help、Ctrl+C 都由它自己处理好，我们只管退出码。
    """
    _setup_stdout()
    try:
        app(args=argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        # SystemExit 里塞字符串的情况（理论上不该出现）
        print(code, file=sys.stderr)
        return 1
    finally:
        # 兜底：异常退出时也别把半行动画留在屏幕上
        ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
