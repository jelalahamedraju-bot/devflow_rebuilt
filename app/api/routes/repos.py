import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import Issue, Repository
from app.db.session import get_db
from app.schemas.repos import (
    IssueOut,
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

    result = await sync_repository(db, repo, RepoSyncRequest(limit=payload.initial_sync_limit))
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
