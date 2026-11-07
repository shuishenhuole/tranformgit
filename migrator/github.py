"""GitHub REST API v3 封装。

文档：https://docs.github.com/rest

Token 权限要求：
- Classic：`repo`（私有仓库）或 `public_repo`（仅公开仓库）
- Fine-grained：Contents(RW)、Issues(RW)、Metadata(R)，要往组织里建仓库
  还需要 Administration(RW)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .http import ApiError, HttpClient

GITHUB_API = "https://api.github.com"
# 上传 Release 附件走独立域名，这是 GitHub 的固定要求
GITHUB_UPLOAD_API = "https://uploads.github.com"


class GithubClient(HttpClient):
    """GitHub API 客户端。

    Args:
        token: 个人访问令牌，必填（不带认证的 GitHub API 额度只有 60 次/小时）。
        api: 便于指向 GH Enterprise，默认官方地址。
    """

    def __init__(self, token: str, *, api: str = GITHUB_API) -> None:
        super().__init__(
            api,
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                # 固定 API 版本，避免 GitHub 改默认行为把脚本打挂
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "reposor-trans/0.1",
            },
        )
        self.token = token

    # ---------------------------------------------------------------- 账号
    def whoami(self) -> dict[str, Any]:
        """当前 token 对应的用户，用来确定默认的目标 owner。"""
        return self.get("/user")

    def get_repo(self, owner: str, name: str) -> dict[str, Any] | None:
        """取仓库；不存在或无权访问返回 None（不抛异常）。"""
        return self.get(f"/repos/{owner}/{name}", allow_404=True)

    # ---------------------------------------------------------------- 仓库
    def create_repo(
        self,
        name: str,
        *,
        private: bool,
        description: str = "",
        homepage: str = "",
        org: str | None = None,
    ) -> dict[str, Any]:
        """创建仓库。

        auto_init=False 很关键：不要生成初始 commit，否则我们 push 时
        会出现两条无关历史，还得额外 merge。
        org 非空时走组织接口，否则建到当前用户名下。
        """
        payload = {
            "name": name,
            "private": private,
            # GitHub 简介上限 350 字符
            "description": description[:350],
            "homepage": homepage or "",
            "has_issues": True,
            "has_projects": False,
            "has_wiki": False,
            "auto_init": False,
        }
        path = f"/orgs/{org}/repos" if org else "/user/repos"
        return self.post(path, json=payload)

    def update_repo(self, owner: str, name: str, **fields: Any) -> dict[str, Any]:
        """局部更新仓库，迁移里主要用来改 default_branch。"""
        return self.patch(f"/repos/{owner}/{name}", json=fields)

    # ---------------------------------------------------------------- 标签
    def list_labels(self, owner: str, name: str) -> list[dict[str, Any]]:
        return self.paginate(f"/repos/{owner}/{name}/labels")

    def ensure_label(
        self, owner: str, name: str, label: str, color: str, description: str = ""
    ) -> None:
        """确保标签存在，已存在就跳过。

        为什么不直接用「建 Issue 时顺带传标签」？那样 GitHub 会自动创建
        标签但颜色是默认灰，这里显式创建才能把 Gitee 的颜色带过来。
        """
        try:
            self.post(
                f"/repos/{owner}/{name}/labels",
                json={
                    "name": label[:50],  # GitHub 标签名上限 50
                    "color": color,  # 6 位十六进制，不带 #
                    "description": (description or "")[:100],
                },
            )
        except ApiError as exc:
            # 422 = 同名标签已存在，属于预期情况
            if exc.status != 422:
                raise

    # --------------------------------------------------------------- Issue
    def list_issues(self, owner: str, name: str) -> list[dict[str, Any]]:
        """取全部 Issue，过滤掉 PR（GitHub 的 issues 接口会把 PR 一起返回）。"""
        issues = self.paginate(f"/repos/{owner}/{name}/issues", params={"state": "all"})
        return [i for i in issues if not i.get("pull_request")]

    def create_issue(
        self,
        owner: str,
        name: str,
        *,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """建 Issue，返回体里的 number 是 GitHub 新分配的编号。

        注意：新编号与 Gitee 原编号不同，后续评论要挂到这个新编号上。
        """
        # 标题为空会被 API 拒绝，兜一个占位
        payload: dict[str, Any] = {"title": title[:250] or "untitled", "body": body}
        if labels:
            payload["labels"] = labels
        return self.post(f"/repos/{owner}/{name}/issues", json=payload)

    def close_issue(self, owner: str, name: str, number: int) -> None:
        """把 Issue 置为已关闭。

        GitHub 不允许在创建时直接指定 closed 状态，只能先建成 open 再改，
        所以迁移已关闭的 Issue 会多一次请求。
        """
        self.patch(
            f"/repos/{owner}/{name}/issues/{number}",
            json={"state": "closed", "state_reason": "completed"},
        )

    def create_comment(
        self, owner: str, name: str, number: int, body: str
    ) -> dict[str, Any]:
        """在指定 Issue 下追加评论。"""
        return self.post(
            f"/repos/{owner}/{name}/issues/{number}/comments", json={"body": body}
        )

    # ------------------------------------------------------------- Release
    def create_release(
        self,
        owner: str,
        name: str,
        *,
        tag_name: str,
        title: str,
        body: str = "",
        prerelease: bool = False,
        target_commitish: str | None = None,
    ) -> dict[str, Any]:
        """基于已存在的 tag 创建 Release。

        tag 必须已经在仓库里（代码迁移阶段推上去的），否则 422。
        """
        payload: dict[str, Any] = {
            "tag_name": tag_name,
            "name": title or tag_name,
            "body": body,
            "prerelease": prerelease,
            "draft": False,
        }
        if target_commitish:
            payload["target_commitish"] = target_commitish
        return self.post(f"/repos/{owner}/{name}/releases", json=payload)

    def upload_release_asset(
        self, owner: str, name: str, release_id: int, file_path: Path, asset_name: str
    ) -> dict[str, Any]:
        """上传 Release 附件。

        附件走 uploads.github.com，且是原始字节流（不是 multipart），
        文件名通过 query 的 name 参数传。
        """
        url = f"{GITHUB_UPLOAD_API}/repos/{owner}/{name}/releases/{release_id}/assets"
        headers = {
            "Content-Type": "application/octet-stream",
            # 显式给长度，避免 httpx 用 chunked 编码（GitHub 不接受）
            "Content-Length": str(file_path.stat().st_size),
        }
        try:
            with file_path.open("rb") as fh:
                resp = self.client.post(
                    url,
                    params={"name": asset_name},
                    content=fh,  # httpx 支持直接传文件对象，会分块发送
                    headers=headers,
                    timeout=httpx.Timeout(900.0, connect=15.0),  # 大文件给足时间
                )
                resp.read()
        except httpx.HTTPError as exc:
            raise ApiError(f"上传 asset 失败 {asset_name}: {exc}", url=url) from exc

        if resp.status_code >= 400:
            raise ApiError(
                f"上传 asset 失败 {asset_name} -> {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
                url=url,
            )
        return resp.json()


def download_to(url: str, dest: Path, *, token: str | None = None) -> None:
    """通用流式下载（预留：从 GitHub 侧取资产时使用）。"""
    headers = {"User-Agent": "reposor-trans/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(
            headers=headers, follow_redirects=True, trust_env=True
        ) as client:
            with client.stream(
                "GET", url, timeout=httpx.Timeout(600.0, connect=15.0)
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
