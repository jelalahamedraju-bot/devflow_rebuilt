import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RepoSyncRequest(BaseModel):
    """一次同步要做什么。第一版只有 Issue；PR / CI 的开关以后再加。"""

    sync_issues: bool = True
    limit: int = Field(default=30, ge=1, le=100)


class RepoSyncResponse(BaseModel):
    repo_id: uuid.UUID
    status: str
    # 本次处理了多少个远端对象（不是新增行数）
    synced: dict[str, int]


class RepoConnectRequest(BaseModel):
    owner: str
    repo: str
    # 首次连接只取少量数据，避免一上来扫大仓库
    initial_sync_limit: int = Field(default=5, ge=1, le=50)


class RepoConnectResponse(BaseModel):
    repo_id: uuid.UUID
    full_name: str
    connected: bool
    sync_status: str
    synced: dict[str, int]


class IssueOut(BaseModel):
    model_config = {"from_attributes": True}

    number: int
    title: str
    state: str
    labels: list[str]
    author: str | None
    assignees: list[str]
    updated_at: datetime | None
