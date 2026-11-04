"""同步核心：GitHub -> PostgreSQL/SQLite。

所有入口（首次连接、手动刷新，以后还有定时任务、Webhook）都调用 sync_repository，
字段映射和「已存在就更新」的规则只在这里维护一份。

这里只做结构化入库，不碰 Embedding / 向量库（那是第 07 章的事）。
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import Issue, PRFile, PRReviewComment, PullRequest, Repository, WorkflowRun
from app.schemas.repos import RepoSyncRequest, RepoSyncResponse
from app.services.github.actions import get_workflow_run_logs, list_workflow_run_jobs, list_workflow_runs
from app.services.github.issues import list_issues
from app.services.github.pull_requests import (
    list_pull_request_files,
    list_pull_request_review_comments,
    list_pull_requests,
)


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
        synced["issues"] = await _sync_issues(db, repo, payload.limit)

    if payload.sync_pull_requests:
        synced["pull_requests"] = await _sync_pull_requests(db, repo, payload.limit)

    if payload.sync_workflow_runs:
        synced["workflow_runs"] = await _sync_workflow_runs(db, repo, payload.limit)

    repo.last_sync_at = datetime.now(UTC)
    # 唯一的提交点：前面任何一步报错，都不会写入半截数据
    db.commit()

    return RepoSyncResponse(repo_id=repo.id, status="completed", synced=synced)


async def _sync_issues(db: Session, repo: Repository, limit: int) -> int:
    items = await list_issues(repo.owner, repo.name, limit=limit)
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
    return len(items)


async def _sync_pull_requests(db: Session, repo: Repository, limit: int) -> int:
    items = await list_pull_requests(repo.owner, repo.name, limit=limit)
    for item in items:
        # ① PR 本体：和 Issue 完全同一套规则（repo_id + GitHub ID，有则更新、无则新增）
        pr = (
            db.query(PullRequest)
            .filter(PullRequest.repo_id == repo.id, PullRequest.github_pr_id == item["id"])
            .one_or_none()
        )
        if pr is None:
            pr = PullRequest(repo_id=repo.id, github_pr_id=item["id"])
            db.add(pr)

        pr.number = item["number"]
        pr.title = item.get("title") or ""
        pr.body = item.get("body")
        pr.state = item.get("state") or "open"
        pr.author = login_of(item.get("user"))
        pr.base_branch = (item.get("base") or {}).get("ref")
        pr.head_branch = (item.get("head") or {}).get("ref")
        pr.merged_at = parse_github_datetime(item.get("merged_at"))
        pr.created_at = parse_github_datetime(item.get("created_at"))
        pr.updated_at = parse_github_datetime(item.get("updated_at"))
        # flush：把这个 PR 先发给数据库（还没提交），这样新 PR 的 pr.id 就有值了，
        # 下面的文件和评论要用 pr.id 作为外键
        db.flush()

        # ② 子资源：每个 PR 要再请求 2 次 GitHub（所以 PR 多时同步会比较慢）
        files = await list_pull_request_files(repo.owner, repo.name, pr.number)
        comments = await list_pull_request_review_comments(repo.owner, repo.name, pr.number)

        # ③ 整组替换：先删掉这个 PR 的旧文件/旧评论，再写入最新的。
        #    文件没有 GitHub ID，没法「按 ID 找到再更新」；而且作者撤掉的文件也必须从本地消失。
        #    代价：如果某次拉取失败返回了空列表，本地也会被清空（生产环境要更细的失败处理）。
        db.query(PRFile).filter(PRFile.pr_id == pr.id).delete()
        for f in files:
            db.add(
                PRFile(
                    pr_id=pr.id,
                    filename=f.get("filename") or "",
                    status=f.get("status") or "modified",
                    additions=f.get("additions") or 0,
                    deletions=f.get("deletions") or 0,
                    patch=f.get("patch"),  # 二进制/超大文件没有 patch，保留 None
                )
            )

        db.query(PRReviewComment).filter(PRReviewComment.pr_id == pr.id).delete()
        for c in comments:
            db.add(
                PRReviewComment(
                    pr_id=pr.id,
                    github_comment_id=c.get("id"),
                    body=c.get("body"),
                    path=c.get("path"),
                    # 代码更新后评论对应的行可能已不存在，这时 line 为空，退回 original_line
                    line=c.get("line") or c.get("original_line"),
                    author=login_of(c.get("user")),
                    created_at=parse_github_datetime(c.get("created_at")),
                    updated_at=parse_github_datetime(c.get("updated_at")),
                )
            )
    return len(items)


def _summarize_job(job: dict) -> dict:
    """只保留需要的字段，不把 GitHub 的整个响应原样存进去。"""
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "html_url": job.get("html_url"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "steps": [
            {
                "number": step.get("number"),
                "name": step.get("name"),
                "status": step.get("status"),
                "conclusion": step.get("conclusion"),
            }
            for step in job.get("steps", [])
        ],
    }


async def _sync_workflow_runs(db: Session, repo: Repository, limit: int) -> int:
    items = await list_workflow_runs(repo.owner, repo.name, limit=limit)
    for item in items:
        # ① Run 本体：同一套「repo_id + GitHub ID，有则更新、无则新增」
        run = (
            db.query(WorkflowRun)
            .filter(WorkflowRun.repo_id == repo.id, WorkflowRun.github_run_id == item["id"])
            .one_or_none()
        )
        if run is None:
            run = WorkflowRun(repo_id=repo.id, github_run_id=item["id"])
            db.add(run)

        run.name = item.get("name") or "workflow"
        run.status = item.get("status") or "unknown"
        run.conclusion = item.get("conclusion")  # 进行中时为 None，跑完后才有值
        run.html_url = item.get("html_url")
        run.created_at = parse_github_datetime(item.get("created_at"))
        run.updated_at = parse_github_datetime(item.get("updated_at"))

        # ② Jobs + Steps：整理后整组覆盖
        jobs = await list_workflow_run_jobs(repo.owner, repo.name, item["id"])
        run.jobs = [_summarize_job(job) for job in jobs]

        # ③ 只有失败的运行才下载日志：成功日志量大、重复多，排查价值低
        if run.conclusion == "failure":
            try:
                run.logs_text = await get_workflow_run_logs(repo.owner, repo.name, item["id"])
            except Exception:
                # 日志是「锦上添花」：下载失败（没 Token、日志已过期被删）不能拖垮整次同步。
                # 运行记录本身照样保存，只是没有日志。（和「同步不依赖 RAG」是同一个原则）
                pass
    return len(items)
