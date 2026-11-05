"""基于 httpx 的 HTTP 客户端基类。

Gitee 和 GitHub 两个 API 封装都继承它，公共能力集中在这里：
- 统一重试：网络抖动、5xx、429 自动重试，其余错误直接抛 ApiError
- 限流退避：GitHub 触发限流会返回 403 + X-RateLimit-Reset，按重置时间等待
- 分页遍历：两边都用 page/per_page，抽成 paginate() 避免每处手写循环
- 404 容忍：查询「可能不存在」的资源时用 allow_404，避免用异常做流程控制
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# 这些状态码属于「重试可能成功」，4xx 里除 429 外基本都是请求本身有问题
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    """API 调用失败。

    额外带上 status / url / body，方便上层做分支处理，
    例如 422 表示「资源已存在」、404 表示「没有这个仓库」。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body


class HttpClient:
    """同步 HTTP 客户端基类。

    Args:
        base_url: API 根地址，例如 https://gitee.com/api/v5
        headers: 所有请求都会带上的头（认证信息通常放这里）
        timeout: 单次请求超时（秒）
        max_retries: 最多尝试次数（含首次）
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.Client(
            headers=headers or None,
            # 连接超时单独给短一点的 15s：DNS 或网络不通时不必干等 30s
            timeout=httpx.Timeout(timeout, connect=15.0),
            # GitHub 的资产下载、建仓库等接口会 302，必须跟随
            follow_redirects=True,
            # 尊重 HTTP(S)_PROXY 环境变量，国内访问 GitHub 常需要
            trust_env=True,
        )

    # ------------------------------------------------------------ 请求核心
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        content: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_404: bool = False,
    ) -> Any:
        """发起请求并解析 JSON；失败时抛 ApiError。

        Args:
            path: 相对路径（拼到 base_url 后）或完整 URL。
            allow_404: 为 True 时 404 返回 None 而不是抛异常，
                用于「查询可能存在的资源」这类场景。
            timeout: 覆盖本次请求的超时；None 表示用客户端默认值。

        Returns:
            解析后的 JSON；204 或空响应返回 None；非 JSON 响应返回文本。
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                resp = self.client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    content=content,
                    headers=headers,
                    timeout=httpx.Timeout(timeout, connect=15.0) if timeout else None,
                )
            except httpx.HTTPError as exc:
                # 连接失败、超时、TLS 错误等，统一按可重试处理
                last_error = exc
                if attempt == self.max_retries - 1:
                    raise ApiError(f"{method} {url} 请求失败: {exc}", url=url) from exc
                time.sleep(min(2**attempt, 30))  # 指数退避，封顶 30s
                continue

            # 限流和 5xx 走同一套退避逻辑
            if resp.status_code in RETRYABLE_STATUS or (
                resp.status_code == 403 and self._is_rate_limited(resp)
            ):
                if attempt == self.max_retries - 1:
                    raise ApiError(
                        f"{method} {url} 重试耗尽 -> {resp.status_code}",
                        status=resp.status_code,
                        url=url,
                        body=resp.text[:500],
                    )
                time.sleep(self._retry_delay(resp, attempt))
                continue

            if resp.status_code == 404 and allow_404:
                return None

            if resp.status_code >= 400:
                # 只截前 500 字符：错误体可能是一整页 HTML，没必要全塞进异常
                raise ApiError(
                    f"{method} {url} -> {resp.status_code}: {resp.text[:500]}",
                    status=resp.status_code,
                    url=url,
                    body=resp.text,
                )

            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                # 少数接口返回纯文本，原样给出
                return resp.text

        raise ApiError(f"{method} {url} 失败: {last_error}", url=url)

    # 常用方法的糖衣，省得每处都写 request("GET", ...)
    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def close(self) -> None:
        """释放连接池。批量迁移时建议用 with 语句自动关闭。"""
        self.client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- 分页
    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
        max_pages: int = 500,
    ) -> list[dict[str, Any]]:
        """按 page/per_page 拉全部结果，直到返回条数不足一页。

        Gitee 和 GitHub 都用这套参数，per_page 上限都是 100。
        max_pages 是保险丝：接口异常时不会陷入无限循环。
        """
        results: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            query = dict(params or {})
            query.update({"page": page, "per_page": per_page})
            data = self.get(path, params=query)
            if not data:
                break
            if isinstance(data, dict):
                # 个别接口会把列表包一层 {"items": [...]} / {"data": [...]}
                data = data.get("items") or data.get("data") or []
            results.extend(data)
            # 拿到的不足一页，说明已经是最后一页
            if len(data) < per_page:
                break
            page += 1
        return results

    # ------------------------------------------------------------ 限流判断
    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        """403 到底是被限流还是没权限？两种信号都认。"""
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        return "rate limit" in resp.text[:500].lower()

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> int:
        """算出下次重试前该睡多久（秒）。

        优先级：Retry-After 头 > GitHub 的 X-RateLimit-Reset > 指数退避。
        上限 300s，避免真被限流时卡太久。
        """
        retry_after = resp.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(int(retry_after), 120)
        if resp.status_code == 403:
            reset = resp.headers.get("X-RateLimit-Reset", "")
            if reset.isdigit():
                return max(1, min(int(reset) - int(time.time()) + 1, 300))
        return min(2**attempt, 60)
