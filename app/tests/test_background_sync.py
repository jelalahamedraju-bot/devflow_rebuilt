"""6.4 自动更新：后台同步入口、定时任务、Webhook。

三个入口最终都调用同一个 sync_repository —— 「入口可以增加，同步规则必须收口」。
"""

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.models import Issue
from app.schemas.repos import RepoSyncRequest
from app.tests.conftest import make_repo


@pytest.fixture
def session_factory(db, monkeypatch):
    """后台任务和 Webhook 会自己开 Session（SessionLocal()）。测试里让它们连到测试数据库。"""
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.services.repos_sync.SessionLocal", factory, raising=False)
    monkeypatch.setattr("app.services.scheduler.SessionLocal", factory, raising=False)
    monkeypatch.setattr("app.api.routes.webhooks.SessionLocal", factory, raising=False)
    return factory


# ---------- ① 后台同步入口 ----------


# 13. 只给 repo_id，它自己开 Session、找仓库、调用 sync_repository、关 Session
async def test_sync_repository_by_id_opens_its_own_session(db, session_factory, monkeypatch):
    from app.services.repos_sync import sync_repository_by_id

    repo = make_repo(db)

    async def one_issue(*_a, **_k):
        return [{"id": 6078323038, "number": 86, "title": "t", "state": "open"}]

    monkeypatch.setattr("app.services.repos_sync.list_issues", one_issue)

    result = await sync_repository_by_id(repo.id, RepoSyncRequest())

    assert result.synced["issues"] == 1
    db.expire_all()  # 数据是另一个 Session 写的，让当前 Session 重新读
    assert db.query(Issue).filter(Issue.repo_id == repo.id).count() == 1


# 14. 仓库不存在（比如刚被删掉）时返回 None，而不是抛异常让后台任务崩掉
async def test_sync_repository_by_id_unknown_repo_returns_none(db, session_factory):
    import uuid

    from app.services.repos_sync import sync_repository_by_id

    assert await sync_repository_by_id(uuid.uuid4(), RepoSyncRequest()) is None


# ---------- ② 定时任务 ----------


# 15. 一轮定时同步：所有仓库都同步一遍；某个仓库失败不影响后面的仓库
async def test_scheduler_round_continues_after_one_repo_fails(db, session_factory, monkeypatch):
    from app.services import scheduler

    bad = make_repo(db, "team", "broken")
    good = make_repo(db, "team", "fine")
    synced = []

    async def fake_sync_by_id(repo_id, payload):
        if repo_id == bad.id:
            raise RuntimeError("GitHub 503")
        synced.append(repo_id)

    monkeypatch.setattr(scheduler, "sync_repository_by_id", fake_sync_by_id)

    await scheduler.run_sync_round()

    assert synced == [good.id]


# ---------- ③ Webhook ----------

SECRET = "test-webhook-secret"


def sign(body: bytes) -> str:
    """模拟 GitHub：用共享密钥对原始请求体做 HMAC-SHA256 签名。"""
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def webhook(db, session_factory, monkeypatch):
    """只挂 Webhook 路由的小 app（不触发 main.py 的启动逻辑），并记录安排了哪些后台同步。"""
    from app.api.routes import webhooks

    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    scheduled = []

    async def fake_sync_by_id(repo_id, payload):
        scheduled.append(repo_id)

    monkeypatch.setattr(webhooks, "sync_repository_by_id", fake_sync_by_id)

    app = FastAPI()
    app.include_router(webhooks.router, prefix="/api/webhooks")
    client = TestClient(app)

    def post(payload: dict, event="issues", signature=None):
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature if signature is not None else sign(body),
            "Content-Type": "application/json",
        }
        return client.post("/api/webhooks/github", content=body, headers=headers)

    return post, scheduled


# 16. 签名对、仓库已连接、事件受支持 → 安排一次后台同步
def test_webhook_valid_event_schedules_sync(db, webhook):
    post, scheduled = webhook
    repo = make_repo(db, "jelalahamedraju-bot", "devflow_rebuilt")

    resp = post({"action": "closed", "repository": {"full_name": repo.full_name}})

    assert resp.status_code == 200
    assert resp.json()["action"] == "sync_scheduled"
    assert scheduled == [repo.id]


# 17. 签名不对（有人伪造请求）→ 401，不同步
def test_webhook_bad_signature_is_rejected(db, webhook):
    post, scheduled = webhook
    make_repo(db, "jelalahamedraju-bot", "devflow_rebuilt")

    resp = post({"repository": {"full_name": "jelalahamedraju-bot/devflow_rebuilt"}}, signature="sha256=deadbeef")

    assert resp.status_code == 401
    assert scheduled == []


# 18. 没连接过的仓库 → 接收但不同步
def test_webhook_unknown_repo_is_ignored(db, webhook):
    post, scheduled = webhook

    resp = post({"repository": {"full_name": "someone/else"}})

    assert resp.status_code == 200
    assert resp.json()["accepted"] is False
    assert scheduled == []


# 19. 不关心的事件（比如有人点了 Star）→ 忽略
def test_webhook_unsupported_event_is_ignored(db, webhook):
    post, scheduled = webhook
    repo = make_repo(db, "jelalahamedraju-bot", "devflow_rebuilt")

    resp = post({"repository": {"full_name": repo.full_name}}, event="star")

    assert resp.json()["action"] == "ignored"
    assert scheduled == []

