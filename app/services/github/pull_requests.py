from app.services.github.client import GitHubClient


async def list_pull_requests(owner: str, repo: str, token: str | None = None, limit: int = 30) -> list[dict]:
    """PR 列表。state=all：打开、关闭、已合并的都要；最多取 limit 条。"""
    client = GitHubClient(token)
    return await client.get_paginated(f"/repos/{owner}/{repo}/pulls", {"state": "all"}, limit=limit)


async def list_pull_request_files(owner: str, repo: str, number: int, token: str | None = None) -> list[dict]:
    """某个 PR 改了哪些文件，以及每个文件的 patch。要全部取完（不设 limit），否则文件会丢。"""
    client = GitHubClient(token)
    return await client.get_paginated(f"/repos/{owner}/{repo}/pulls/{number}/files")


async def list_pull_request_review_comments(owner: str, repo: str, number: int, token: str | None = None) -> list[dict]:
    """针对 Diff 行的审查评论。注意：不包含 PR 下面的普通留言，也不包含整体 Review 结论。"""
    client = GitHubClient(token)
    return await client.get_paginated(f"/repos/{owner}/{repo}/pulls/{number}/comments")
