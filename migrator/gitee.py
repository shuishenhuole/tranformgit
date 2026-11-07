"""Gitee OpenAPI v5 封装（只覆盖迁移用到的那几个接口）。

接口文档：https://gitee.com/api/v5/swagger

实测注意点：
- `/repos/{owner}/{repo}` 匿名可读公开仓库；
- `/users/{owner}/repos`、`/orgs/{owner}/repos` 这类列表接口匿名基本都返回
  404，所以批量迁移必须配 GITEE_TOKEN。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .http import ApiError, HttpClient

GITEE_API = "https://gitee.com/api/v5"


class GiteeClient(HttpClient):
    """Gitee API 客户端。

    Args:
        token: 私人令牌；传 None 时只能读公开资源，且列表接口大概率不可用。
        api: 便于指向私有部署，默认官方地址。
    """

    def __init__(self, token: str | None = None, *, api: str = GITEE_API) -> None:
        super().__init__(
            api,
            {
                "Accept": "application/json",
                "User-Agent": "reposor-trans/0.1",
            },
        )
        self.token = token

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """统一在 query 里补 access_token。

        Gitee 同时支持 Header 和 query 两种认证方式，这里用 query 是因为
        它对「带 token 下载附件」这类跳转场景更省事。
        setdefault 保证调用方已显式指定的值不被覆盖。
        """
        params = dict(kwargs.get("params") or {})
        if self.token:
            params.setdefault("access_token", self.token)
        kwargs["params"] = params
        return super().request(method, path, **kwargs)

    # ---------------------------------------------------------------- 账号
    def whoami(self) -> dict[str, Any]:
        """当前 token 对应的用户，用于确定「默认迁移谁名下的仓库」。"""
        return self.get("/user")

    def list_orgs(self) -> list[dict[str, Any]]:
        """列出当前 token 所属的组织。

        组织仓库不在 /user/repos 的返回里，要单独去每个组织下取，
        所以要先把组织清单拿到手。接口不可用时按「没有组织」处理。
        """
        try:
            return self.paginate("/user/orgs") or []
        except ApiError as exc:
            if exc.status in (403, 404):
                return []
            raise

    # ---------------------------------------------------------------- 仓库
    def get_repo(self, owner: str, repo: str) -> dict[str, Any] | None:
        """取仓库元信息；不存在或无权限时返回 None。"""
        return self.get(f"/repos/{owner}/{repo}", allow_404=True)

    def list_repos(
        self, owner: str, *, include_forks: bool = False
    ) -> list[dict[str, Any]]:
        """列出账号或组织下的仓库。

        Gitee 没有统一的「按 owner 列仓库」接口：普通用户走 /users/{owner}，
        组织走 /orgs/{owner}，而两者对不存在的 owner 都返回 404，
        所以只能先试用户、404 再试组织。
        """
        try:
            repos = self.paginate(
                f"/users/{owner}/repos",
                params={"type": "all", "sort": "updated", "direction": "desc"},
            )
        except ApiError as exc:
            if exc.status != 404:
                raise
            try:
                repos = self.paginate(
                    f"/orgs/{owner}/repos",
                    params={"type": "all", "sort": "updated", "direction": "desc"},
                )
            except ApiError as org_exc:
                # 两条路都断了：把「大概率是没配 token」这个线索直接写进报错
                raise ApiError(
                    f"无法列出 {owner} 的仓库（Gitee 的列表接口基本都要认证，"
                    f"请确认 GITEE_TOKEN 有效且该账号/组织存在）：{org_exc}",
                    status=org_exc.status,
                ) from org_exc
        # fork 默认跳过，避免把别人的仓库整片搬过来
        return [r for r in repos if include_forks or not r.get("fork")]

    def list_all_repos(
        self, *, include_forks: bool = False, log: Any = None
    ) -> list[dict[str, Any]]:
        """列出当前 token 能访问的全部仓库：个人仓库 + 所属组织的仓库。

        这就是「一键拿到所有 Gitee 仓库」的实现：
        /user/repos 只返回个人名下的仓库，组织仓库要逐个组织去拉，
        所以先取 /user/orgs，再对每个组织调 /orgs/{org}/repos。
        某个组织拉取失败只跳过该组织，不影响其余结果。

        Args:
            include_forks: 是否包含 fork 仓库。
            log: 可选日志函数，用来提示某个组织拉取失败。

        Returns:
            去重后的仓库列表（按 full_name 去重）。
        """
        if not self.token:
            raise ApiError("列出全部仓库需要 GITEE_TOKEN，请先配置")

        repos = self.paginate(
            "/user/repos",
            params={"type": "all", "sort": "updated", "direction": "desc"},
        )

        for org in self.list_orgs():
            org_path = org.get("login") or org.get("path") or org.get("name")
            if not org_path:
                continue
            try:
                repos.extend(
                    self.paginate(
                        f"/orgs/{org_path}/repos",
                        params={"type": "all", "sort": "updated", "direction": "desc"},
                    )
                )
            except ApiError as exc:
                # 没有组织仓库权限是常见的，提示一下继续走
                if log:
                    log(f"! 跳过组织 {org_path}：{exc}")

        # 同一仓库可能既算个人又算组织（迁移来的 fork），按 full_name 去重
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for repo in repos:
            key = repo.get("full_name") or repo.get("path") or ""
            if key and key in seen:
                continue
            seen.add(key)
            if not include_forks and repo.get("fork"):
                continue
            unique.append(repo)
        return unique

    # ---------------------------------------------------------------- Issue
    def list_issues(self, owner: str, repo: str) -> list[dict[str, Any]]:
        """取全部 Issue（含已关闭），按创建时间升序。

        升序是为了让 GitHub 那边的编号顺序与源仓库大致一致。
        """
        issues = self.paginate(
            f"/repos/{owner}/{repo}/issues",
            params={"state": "all", "sort": "created", "direction": "asc"},
        )
        # Gitee 的 issues 接口可能混入 PR，GB 那边的分页也会混，统一过滤
        return [i for i in issues if not i.get("pull_request")]

    def list_issue_comments(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        """取某个 Issue 的全部评论，按时间升序以便保持对话顺序。"""
        return self.paginate(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"sort": "created", "direction": "asc"},
        )

    # --------------------------------------------------------------- 发行版
    def list_releases(self, owner: str, repo: str) -> list[dict[str, Any]]:
        """取全部 Release。

        老仓库或权限不足时会 403/404，这种情况按「没有 Release」处理，
        不该因为一个可选环节让整个迁移失败。
        """
        try:
            return self.paginate(f"/repos/{owner}/{repo}/releases") or []
        except ApiError as exc:
            if exc.status in (403, 404):
                return []
            raise

    # ---------------------------------------------------------------- 下载
    def download(self, url: str, dest: Path) -> None:
        """流式下载到本地文件（用于 Release 附件中转）。

        用 stream + iter_bytes 而不是一次性读进内存，
        避免几百 MB 的安装包把内存吃满。
        """
        params = {"access_token": self.token} if self.token else None
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.client.stream(
                "GET", url, params=params, timeout=httpx.Timeout(600.0, connect=15.0)
            ) as resp:
                if resp.status_code >= 400:
                    raise ApiError(
                        f"下载失败 {url} -> {resp.status_code}",
                        status=resp.status_code,
                        url=url,
                    )
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                        if chunk:
                            fh.write(chunk)
        except httpx.HTTPError as exc:
            raise ApiError(f"下载失败 {url}: {exc}", url=url) from exc
