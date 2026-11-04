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
