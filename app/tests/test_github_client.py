"""GitHubClient 的分页测试。

GitHub 列表接口每页最多 100 条。PR #81 实际改了 117 个文件，
只取第一页会悄悄丢掉 17 个——这就是这个测试要防住的 bug。
"""

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
