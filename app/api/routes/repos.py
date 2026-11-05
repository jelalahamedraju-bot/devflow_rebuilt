import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
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
from app.services.git_repo import effective_api_base_url, infer_provider, inspect_local_repository
from app.services.project_indexing import checkout_and_scan_by_id
from app.services.repos_sync import sync_repository

router = APIRouter(prefix="/repos", tags=["repos"])


@router.post("/connect", response_model=RepoConnectResponse)
async def connect_repo(payload: RepoConnectRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """入口 1：首次连接仓库。

    ① 认身份：local 模式跑 4 条 git 命令；否则用填的 owner/repo
    ② 认平台：github 还是 github_compatible，API 去哪个地址
    ③ 问 GitHub：仓库真的存在吗？默认分支、clone 地址是什么
    ④ 存仓库表并 commit（拿到 repo.id）
    ⑤ 同步 Issue / PR / CI（等它完成）
    ⑥ 后台：准备代码（managed 下载 / local 检查）+ 项目预扫描（不等它完成）
    """
    owner, name = payload.owner, payload.repo
    checkout_mode = "managed"
    local_path: str | None = None
    remote_host: str | None = None
    branch_hint: str | None = None

    if payload.local_path:
        root, remote = inspect_local_repository(payload.local_path)
        owner, name, remote_host, branch_hint = remote.owner, remote.repo, remote.host, remote.default_branch
        checkout_mode, local_path = "local", str(root)
    elif not owner or not name:
        raise HTTPException(status_code=400, detail="请提供本地仓库文件夹 local_path，或者 owner + repo")

    provider = infer_provider(payload.provider, remote_host)
    api_base_url = effective_api_base_url(provider, payload.api_base_url, remote_host)

    try:
        info = await GitHubClient(base_url=api_base_url).get_repo(owner, name)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="找不到这个仓库或无权限") from exc

    full_name = info["full_name"]
    if checkout_mode == "managed" and payload.clone_parent_dir:
        parent = Path(payload.clone_parent_dir.strip().strip('"')).expanduser().resolve()
        local_path = str(parent / info["name"])

    repo = db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    if repo is None:
        repo = Repository(owner=info["owner"]["login"], name=info["name"], full_name=full_name)
        db.add(repo)
    repo.provider = provider
    repo.api_base_url = api_base_url
    repo.clone_url = info.get("clone_url")
    repo.checkout_mode = checkout_mode
    repo.local_path = local_path  # managed 且没指定位置时为空，下载后由后台任务填上
    repo.default_branch = branch_hint or info.get("default_branch")
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
    # 下载代码可能要几分钟：放后台，不让用户一直等
    background_tasks.add_task(checkout_and_scan_by_id, repo.id)
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
