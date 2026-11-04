from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import repos
from app.db.models import Base  # 导入 models，让所有表登记到 Base.metadata
from app.db.session import engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时创建缺失的表（学习项目没有 Alembic；生产环境要用正式迁移）
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="DevFlow Rebuild", lifespan=lifespan)
app.include_router(repos.router, prefix="/api")
