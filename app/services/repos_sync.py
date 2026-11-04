"""同步核心：GitHub -> PostgreSQL/SQLite。

所有入口（首次连接、手动刷新，以后还有定时任务、Webhook）都调用 sync_repository，
字段映射和「已存在就更新」的规则只在这里维护一份。

这里只做结构化入库，不碰 Embedding / 向量库（那是第 07 章的事）。
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Issue, Repository
from app.schemas.repos import RepoSyncRequest, RepoSyncResponse
from app.services.github.issues import list_issues


def parse_github_datetime(value: str | None) -> datetime | None:
    """GitHub 时间是 '2024-01-02T00:00:00Z'，把 Z 换成 +00:00 再解析。"""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def login_of(user: dict | None) -> str | None:
    return (user or {}).get("login")


async def sync_repository(db: Session, repo: Repository, payload: RepoSyncRequest) -> RepoSyncResponse:
    synced: dict[str, int] = {}

    if payload.sync_issues:
        items = await list_issues(repo.owner, repo.name, limit=payload.limit)
        for item in items:
            # 关键：先锁定仓库，再用 GitHub 原始 ID 找「是不是同一个 Issue」
            issue = (
                db.query(Issue)
                .filter(Issue.repo_id == repo.id, Issue.github_issue_id == item["id"])
                .one_or_none()
            )
            if issue is None:  # 没有才新增
                issue = Issue(repo_id=repo.id, github_issue_id=item["id"])
                db.add(issue)

            # 有就覆盖字段（open -> closed 就是在这里更新的）
            issue.number = item["number"]
            issue.title = item.get("title") or ""
            issue.body = item.get("body")
            issue.state = item.get("state") or "open"
            issue.labels = [label["name"] for label in item.get("labels", []) if label.get("name")]
            issue.author = login_of(item.get("user"))
            issue.assignees = [login_of(u) for u in item.get("assignees", []) if login_of(u)]
            issue.created_at = parse_github_datetime(item.get("created_at"))
            issue.updated_at = parse_github_datetime(item.get("updated_at"))
            issue.closed_at = parse_github_datetime(item.get("closed_at"))
        synced["issues"] = len(items)

    repo.last_sync_at = datetime.now(UTC)
    # 唯一的提交点：前面任何一步报错，都不会写入半截数据
    db.commit()

    return RepoSyncResponse(repo_id=repo.id, status="completed", synced=synced)
