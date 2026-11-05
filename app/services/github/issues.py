from app.services.github.client import GitHubClient


async def list_issues(owner: str, repo: str, token: str | None = None, base_url: str | None = None, limit: int = 30) -> list[dict]:
    client = GitHubClient(token, base_url)
    # state=all：打开和关闭的都要，这样本地才能看到 open -> closed 的变化
    # 注意：这里只取第一页，没有做分页
    items = await client.get(f"/repos/{owner}/{repo}/issues", {"state": "all", "per_page": limit})
    # GitHub 的 issues 接口会把 PR 也混进来（带 pull_request 字段），过滤掉
    return [item for item in items if "pull_request" not in item]
