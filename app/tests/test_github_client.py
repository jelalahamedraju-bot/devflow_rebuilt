"""GitHubClient 的分页测试。

GitHub 列表接口每页最多 100 条。PR #81 实际改了 117 个文件，
只取第一页会悄悄丢掉 17 个——这就是这个测试要防住的 bug。
"""

import httpx

from app.services.github.client import GitHubClient


def fake_pages(monkeypatch, total):
    """假装 GitHub 上有 total 条数据，按 page/per_page 切页返回；记录请求了哪些页。"""
    requested = []

    async def fake_get(self, path, params=None):
        page, per_page = params["page"], params["per_page"]
        requested.append(page)
        start = (page - 1) * per_page
        return [{"n": i} for i in range(start, min(start + per_page, total))]

    monkeypatch.setattr(GitHubClient, "get", fake_get)
    return requested


async def test_get_paginated_fetches_all_pages(monkeypatch):
    requested = fake_pages(monkeypatch, total=117)

    items = await GitHubClient().get_paginated("/x")

    assert len(items) == 117
    assert requested == [1, 2]  # 第 2 页只有 17 条（不满 100），说明到底了，不再请求第 3 页


async def test_get_paginated_respects_limit(monkeypatch):
    requested = fake_pages(monkeypatch, total=500)

    items = await GitHubClient().get_paginated("/x", limit=5)

    assert len(items) == 5
    assert requested == [1]  # 只要 5 条，就不该把 500 条全拉下来


async def test_get_follows_redirect_for_moved_repository(monkeypatch):
    """仓库改名/转移后，GitHub 回 301 指向新地址；客户端要跟过去，而不是直接报错。

    真实案例：tiangolo/sqlmodel 已转移，请求旧地址得到 301。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/old-owner/old-name":
            return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/42"})
        if request.url.path == "/repositories/42":
            return httpx.Response(200, json={"full_name": "new-owner/new-name"})
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient

    def client_with_fake_network(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_fake_network)

    data = await GitHubClient().get("/repos/old-owner/old-name")

    assert data["full_name"] == "new-owner/new-name"
