import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RepoSyncRequest(BaseModel):
    """一次同步要做什么。每类数据一个开关；limit 是每类最多拉多少条。"""

    sync_issues: bool = True
    sync_pull_requests: bool = True
    sync_workflow_runs: bool = True
    limit: int = Field(default=30, ge=1, le=100)


class RepoSyncResponse(BaseModel):
    repo_id: uuid.UUID
    status: str
    # 本次处理了多少个远端对象（不是新增行数）
    synced: dict[str, int]


class RepoConnectRequest(BaseModel):
    """两种填法二选一：
    - local_path：本地已有的仓库文件夹（local 模式，系统只读，自动认出 owner/repo）
    - owner + repo：系统自己下载（managed 模式），可用 clone_parent_dir 指定下载到哪个文件夹
    """

    owner: str | None = None
    repo: str | None = None
    local_path: str | None = None
    clone_parent_dir: str | None = None
    provider: str | None = None  # github / github_compatible；不填会根据地址自动判断
    api_base_url: str | None = None  # github_compatible 时，公司自建服务的 API 地址
    # 首次连接只取少量数据，避免一上来扫大仓库
    initial_sync_limit: int = Field(default=5, ge=1, le=50)
    sync_issues: bool = True
    sync_pull_requests: bool = True
    sync_workflow_runs: bool = True


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


class PRFileOut(BaseModel):
    model_config = {"from_attributes": True}

    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None


class PRReviewCommentOut(BaseModel):
    model_config = {"from_attributes": True}

    author: str | None
    path: str | None
    line: int | None
    body: str | None


class PullRequestOut(BaseModel):
    model_config = {"from_attributes": True}

    number: int
    title: str
    state: str
    author: str | None
    base_branch: str | None
    head_branch: str | None
    merged_at: datetime | None
    updated_at: datetime | None
    files: list[PRFileOut]
    review_comments: list[PRReviewCommentOut]


class WorkflowRunOut(BaseModel):
    model_config = {"from_attributes": True}

    name: str
    status: str
    conclusion: str | None
    html_url: str | None
    jobs: list[dict]
    logs_text: str | None
    created_at: datetime | None
