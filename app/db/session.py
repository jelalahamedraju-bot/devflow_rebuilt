from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """所有表模型的基类；继承它的类会登记到 Base.metadata。"""


_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

# 数据库引擎：负责真正连接数据库；pool_pre_ping 在取连接时先检查它是否还活着
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect_args)

# Session 工厂：每次调用 SessionLocal() 得到一个新会话；需要手动 commit
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """FastAPI 依赖：每个请求一个 Session，请求结束一定关闭（不会自动提交）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
