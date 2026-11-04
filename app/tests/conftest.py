"""pytest 会自动加载这个文件：这里放所有测试共用的工具（fixture）。"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Repository


@pytest.fixture
def db():
    """每个测试拿到一个干净的数据库会话，测完一定关闭。

    默认用内存 SQLite（快、不依赖外部服务）。
    设置环境变量 TEST_DATABASE_URL 后改用真实 PostgreSQL，例如：
    TEST_DATABASE_URL=postgresql+psycopg://devflow:devflow@127.0.0.1:5433/devflow_test
    SQLite 对类型很宽松（整数永远是 64 位），有些 bug 只有在 PostgreSQL 上才会暴露。
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        engine = create_engine(url)
        Base.metadata.drop_all(engine)  # 每个测试从空库开始
    else:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    # 收尾：不关闭的话，PostgreSQL 上会留下「idle in transaction」的连接，
    # 它持有表锁，下一个测试的 DROP TABLE 会一直等下去（卡死）。
    session.close()
    engine.dispose()


def make_repo(db, owner="openai", name="openai-quickstart-python"):
    repo = Repository(owner=owner, name=name, full_name=f"{owner}/{name}")
    db.add(repo)
    db.commit()
    return repo


@pytest.fixture(autouse=True)
def no_real_github(monkeypatch):
    """所有测试自动生效：同步器里用到的 GitHub 请求函数默认都返回空列表。

    这样任何测试都不会真的联网；每个测试只需要覆盖自己关心的那几个函数。
    """

    async def empty(*_args, **_kwargs):
        return []

    for name in [
        "list_issues",
        "list_pull_requests",
        "list_pull_request_files",
        "list_pull_request_review_comments",
        "list_workflow_runs",
        "list_workflow_run_jobs",
    ]:
        monkeypatch.setattr(f"app.services.repos_sync.{name}", empty, raising=False)
