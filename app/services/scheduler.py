"""定时同步：每隔一段时间把所有已连接的仓库同步一遍。

它是「兜底」：Webhook 可能丢（服务器当时没开、网络断了），定时任务保证最晚下一轮也能补上。
"""

import asyncio
import logging

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

logger = logging.getLogger(__name__)


def _repository_ids() -> list:
    # 先把 id 取出来就关掉这个 Session；每个仓库同步时再各自开自己的 Session
    with SessionLocal() as db:
        return [row.id for row in db.query(Repository.id).all()]


async def run_sync_round() -> None:
    """一轮：逐个仓库同步。一个仓库失败只记警告，不影响后面的仓库。"""
    for repo_id in _repository_ids():
        try:
            await sync_repository_by_id(repo_id, RepoSyncRequest(limit=settings.auto_sync_limit))
        except Exception as exc:
            logger.warning("定时同步失败 repo=%s: %s", repo_id, exc)


async def periodic_repo_sync_loop() -> None:
    """无限循环：同步一轮 → 睡一会 → 再同步一轮。由 main.py 在启动时创建、关闭时取消。"""
    await asyncio.sleep(5)  # 等应用完全启动好再开始
    while True:
        await run_sync_round()
        await asyncio.sleep(max(settings.auto_sync_interval_seconds, 30))  # 至少 30 秒，防止把 GitHub 打爆
