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
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.get(path, headers=self._headers(), params=params)
            # 4xx/5xx 直接抛异常，不能把错误内容当成正常数据存进去
            response.raise_for_status()
            return response.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self.get(f"/repos/{owner}/{repo}")
