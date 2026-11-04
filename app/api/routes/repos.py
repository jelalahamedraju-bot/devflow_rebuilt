import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from app.db.models import Issue, PullRequest, Repository, WorkflowRun
from app.db.session import get_db
from app.schemas.repos import (
    IssueOut,
    PullRequestOut,
    WorkflowRunOut,
    RepoConnectRequest,
    RepoConnectResponse,
    RepoSyncRequest,
    RepoSyncResponse,
)
from app.services.github.client import GitHubClient
from app.services.repos_sync import sync_repository

router = APIRouter(prefix="/repos", tags=["repos"])


@router.post("/connect", response_model=RepoConnectResponse)
async def connect_repo(payload: RepoConnectRequest, db: Session = Depends(get_db)):
    """入口 1：首次连接仓库 -> 确认仓库存在 -> 保存 Repository -> 第一次同步。"""
    try:
        info = await GitHubClient().get_repo(payload.owner, payload.repo)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="GitHub 上找不到这个仓库或无权限") from exc

    full_name = info["full_name"]
    repo = db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    if repo is None:
        repo = Repository(owner=info["owner"]["login"], name=info["name"], full_name=full_name)
        db.add(repo)
    # 先提交，拿到 repo.id；Issue 要用它当外键
    db.commit()

    result = await sync_repository(
        db,
        repo,
        RepoSyncRequest(
            sync_issues=payload.sync_issues,
            sync_pull_requests=payload.sync_pull_requests,
            sync_workflow_runs=payload.sync_workflow_runs,
            limit=payload.initial_sync_limit,
        ),
    )
    return RepoConnectResponse(
        repo_id=repo.id,
        full_name=repo.full_name,
        connected=True,
        sync_status=result.status,
        synced=result.synced,
    )


@router.post("/{repo_id}/sync", response_model=RepoSyncResponse)
async def sync_repo(repo_id: uuid.UUID, payload: RepoSyncRequest, db: Session = Depends(get_db)):
    """入口 2：用户手动刷新。路由只管找仓库，同步规则全在 sync_repository 里。"""
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return await sync_repository(db, repo, payload)


@router.get("/{repo_id}/issues", response_model=list[IssueOut])
def list_issues(repo_id: uuid.UUID, db: Session = Depends(get_db)):
    """读取：只查本地数据库，不请求 GitHub。GitHub 挂了也能看已同步的数据。"""
    return db.query(Issue).filter(Issue.repo_id == repo_id).order_by(Issue.number.desc()).all()


@router.get("/{repo_id}/pull-requests", response_model=list[PullRequestOut])
def list_pull_requests(repo_id: uuid.UUID, db: Session = Depends(get_db)):
    """读取 PR + 改动文件 + 行级评论。同样只查本地数据库。

    selectinload：用一条额外查询把所有 PR 的文件和评论一次性取出来，
    而不是每个 PR 单独查一次（避免「N+1 查询」问题）。
    """
    return (
        db.query(PullRequest)
        .filter(PullRequest.repo_id == repo_id)
        .options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments))
        .order_by(PullRequest.number.desc())
        .all()
    )


@router.get("/{repo_id}/workflow-runs", response_model=list[WorkflowRunOut])
def list_workflow_runs(repo_id: uuid.UUID, db: Session = Depends(get_db)):
    """读取 CI 运行记录（含 Jobs、失败日志），按时间倒序。只查本地数据库。"""
    return (
        db.query(WorkflowRun)
        .filter(WorkflowRun.repo_id == repo_id)
        .order_by(WorkflowRun.created_at.desc())
        .all()
    )


@router.get("/{repo_id}/sync-status")
def sync_status(repo_id: uuid.UUID, db: Session = Depends(get_db)):
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return {
        "repo_id": str(repo.id),
        "status": "completed" if repo.last_sync_at else "not_started",
        "last_sync_at": repo.last_sync_at,
    }
