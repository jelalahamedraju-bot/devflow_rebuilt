import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Repository(Base):
    """一个已连接的 GitHub 仓库。id 是本系统内部 UUID，后面统一叫 repo_id。"""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str] = mapped_column(String(400), unique=True)  # 例如 openai/openai-quickstart-python
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Issue(Base):
    """GitHub Issue 在本地的快照。

    三个 ID 分清楚：
    - id：本表自己的主键
    - repo_id：属于哪个仓库（防止两个仓库的 #42 串线）
    - github_issue_id：GitHub 全局唯一 ID，用来判断「是不是同一个 Issue」
    number 只是用户看到的 #78。
    """

    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("repo_id", "github_issue_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    # GitHub ID 已超过 32 位整数上限（约 21 亿），必须用 BigInteger（64 位）
    github_issue_id: Mapped[int] = mapped_column(BigInteger)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20))
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    assignees: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # GitHub 上这条 Issue 的更新时间，不是我们本地同步的时间
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PullRequest(Base):
    """GitHub PR 在本地的快照。和 Issue 一样：repo_id 防串线，github_pr_id 认人。"""

    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "github_pr_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    github_pr_id: Mapped[int] = mapped_column(BigInteger)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20))  # open / closed（合并后也是 closed）
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    base_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 要合并进去的分支，如 main
    head_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)  # PR 自己的分支
    # 有值 = 已合并；closed 但 merged_at 为空 = 没合并就被关掉了
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 一个 PR 有多个文件、多条评论。cascade：删 PR 时一起删掉它的子资源
    files: Mapped[list["PRFile"]] = relationship(back_populates="pr", cascade="all, delete-orphan")
    review_comments: Mapped[list["PRReviewComment"]] = relationship(back_populates="pr", cascade="all, delete-orphan")


class PRFile(Base):
    """PR 改动的一个文件。没有 GitHub ID，所以每次同步「整组替换」。"""

    __tablename__ = "pr_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pull_requests.id"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(50))  # added / modified / removed / renamed
    additions: Mapped[int] = mapped_column(Integer, default=0)
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    # 代码差异文本；二进制文件或太大的文件 GitHub 不给 patch，所以允许为空
    patch: Mapped[str | None] = mapped_column(Text, nullable=True)

    pr: Mapped[PullRequest] = relationship(back_populates="files")


class PRReviewComment(Base):
    """针对 Diff 某一行的审查评论（不是 PR 下面的普通留言）。"""

    __tablename__ = "pr_review_comments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pull_requests.id"), index=True)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 评论的是哪个文件
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 第几行
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pr: Mapped[PullRequest] = relationship(back_populates="review_comments")


class WorkflowRun(Base):
    """一次 GitHub Actions 运行（CI）。"""

    __tablename__ = "workflow_runs"
    __table_args__ = (UniqueConstraint("repo_id", "github_run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    github_run_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(80))  # queued / in_progress / completed
    conclusion: Mapped[str | None] = mapped_column(String(80), nullable=True)  # success / failure / ...；没跑完时为空
    html_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 每个 Job 及其 Steps 整理后存成 JSON（不单独建表，查询时整体读取即可）
    jobs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 只有失败的运行才有日志；第 07 章进 RAG 前还必须先脱敏、限长
    logs_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
