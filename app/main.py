import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import repos, webhooks
from app.core.config import settings
from app.db.models import Base  # 导入 models，让所有表登记到 Base.metadata
from app.db.session import engine
from app.services.scheduler import periodic_repo_sync_loop


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时创建缺失的表（学习项目没有 Alembic；生产环境要用正式迁移）
    Base.metadata.create_all(bind=engine)

    # 启动定时同步任务；只定义循环不够，必须在这里真正创建任务
    task = asyncio.create_task(periodic_repo_sync_loop()) if settings.auto_sync_enabled else None
    yield
    # 关闭应用时取消任务，否则进程退出时会留下没收尾的后台任务
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="DevFlow Rebuild", lifespan=lifespan)
app.include_router(repos.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api/webhooks")
