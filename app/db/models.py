import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

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
    github_issue_id: Mapped[int] = mapped_column(Integer)
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
