"""项目预扫描的 3 个接口：查状态 / 开始扫描（后台） / 暂缓提醒。"""

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import Repository
from app.db.session import get_db
from app.services.project_indexing import (
    checkout_and_scan_by_id,
    get_project_index_state,
    mark_building,
    snooze_project_index,
)

router = APIRouter(prefix="/repos", tags=["project-index"])


class ProjectIndexStateOut(BaseModel):
    repo_id: str
    status: str  # missing / building / ready / stale / failed
    docs_indexed: int
    last_scan_at: datetime | None
    snoozed_until: datetime | None
    error_message: str | None
    summary: dict


class SnoozeRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90)


def _repo_or_404(db: Session, repo_id: uuid.UUID) -> Repository:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return repo


@router.get("/{repo_id}/memory-index/state", response_model=ProjectIndexStateOut)
def get_state(repo_id: uuid.UUID, db: Session = Depends(get_db)):
    """只查状态，不会启动扫描。"""
    return get_project_index_state(db, _repo_or_404(db, repo_id))


@router.post("/{repo_id}/memory-index/scan", response_model=ProjectIndexStateOut, status_code=202)
def start_scan(repo_id: uuid.UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """202 = 已接受：先把状态改成 building 立刻返回，真正的扫描在后台进行。"""
    repo = _repo_or_404(db, repo_id)
    state = mark_building(db, repo)
    background_tasks.add_task(checkout_and_scan_by_id, repo.id)
    return state


@router.post("/{repo_id}/memory-index/snooze", response_model=ProjectIndexStateOut)
def snooze(repo_id: uuid.UUID, payload: SnoozeRequest | None = None, db: Session = Depends(get_db)):
    return snooze_project_index(db, _repo_or_404(db, repo_id), days=(payload or SnoozeRequest()).days)
