from typing import Any

import httpx

from app.core.config import settings


class GitHubClient:
    """只负责「发 HTTP 请求 + 解析 JSON」。不决定数据存到哪张表。"""

    def __init__(self, token: str | None = None, base_url: str | None = None):
        # 没显式传 token 就用 .env 里的 GITHUB_TOKEN（可以为空）
        self.token = token or settings.github_token
        self.base_url = (base_url or settings.github_api_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            # Token 只放在请求头里，绝不打印到日志
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get(self, path: str, params: dict | None = None) -> Any:
        # follow_redirects：仓库改名/转移后 GitHub 回 301 指向新地址，要跟过去
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20, follow_redirects=True) as client:
            response = await client.get(path, headers=self._headers(), params=params)
            # 4xx/5xx 直接抛异常，不能把错误内容当成正常数据存进去
            response.raise_for_status()
            return response.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self.get(f"/repos/{owner}/{repo}")

    async def get_bytes(self, path: str) -> bytes:
        """下载二进制内容（比如 CI 日志 zip）。

        GitHub 会先回一个 302 跳转到临时下载地址，所以要 follow_redirects。
        httpx 跳到别的域名时会自动去掉 Authorization 头，Token 不会泄露给下载服务器。
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60, follow_redirects=True) as client:
            response = await client.get(path, headers=self._headers())
            response.raise_for_status()
            return response.content

    async def get_paginated(
        self,
        path: str,
        params: dict | None = None,
        limit: int | None = None,
        item_key: str | None = None,
    ) -> list[dict]:
        """一页一页地拉，直到拉完或拉够 limit 条。

        GitHub 列表接口每页最多 100 条；只请求一次会悄悄丢掉后面的数据
        （例如某个 PR 改了 117 个文件，只拿第一页就只剩 100 个）。

        item_key：有的接口不直接返回数组，而是 {"workflow_runs": [...]}，这时告诉我取哪个字段。
        """
        per_page = 100 if limit is None else min(limit, 100)
        items: list[dict] = []
        page = 1
        while True:
            data = await self.get(path, {**(params or {}), "per_page": per_page, "page": page})
            batch = data.get(item_key, []) if item_key else data
            items.extend(batch)
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if len(batch) < per_page:  # 这一页没装满，说明已经是最后一页
                return items
            page += 1
