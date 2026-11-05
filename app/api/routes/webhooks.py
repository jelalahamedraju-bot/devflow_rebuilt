"""GitHub Webhook：GitHub 上一有变化（Issue 关闭、PR 更新、CI 跑完……）就主动通知我们。

注意：我们并不直接用通知里的内容改数据库，而是重新调用一次统一的同步核心。
多一次 GitHub 请求，换来只维护一套字段映射规则。
"""

import hashlib
import hmac

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

router = APIRouter(tags=["webhooks"])

# 只关心会改变我们所存数据的事件；Star、Fork 之类直接忽略
SUPPORTED_EVENTS = {"issues", "pull_request", "pull_request_review_comment", "workflow_run", "push"}


def verify_signature(body: bytes, signature: str | None) -> None:
    """验证请求真的来自 GitHub：

    GitHub 用双方都知道的密钥对「原始请求体」算 HMAC-SHA256，放在 X-Hub-Signature-256 头里。
    我们用同一个密钥再算一遍，对得上才信。别人不知道密钥，就伪造不出正确的签名。
    """
    if not settings.github_webhook_secret:
        return  # 本地开发没配密钥时跳过；部署必须配置
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="缺少 GitHub webhook 签名")
    expected = hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest：比较耗时与内容无关，防止攻击者靠测量响应时间一位一位猜签名
    if not hmac.compare_digest(expected, signature.removeprefix("sha256=")):
        raise HTTPException(status_code=401, detail="GitHub webhook 签名无效")


def find_repo_id(full_name: str):
    with SessionLocal() as db:
        repo = db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
        return repo.id if repo else None


@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    body = await request.body()  # 必须用原始字节验签，不能先解析成 JSON 再序列化
    verify_signature(body, x_hub_signature_256)

    payload = await request.json()
    full_name = (payload.get("repository") or {}).get("full_name")
    if not full_name:
        return {"accepted": False, "reason": "payload 里没有 repository.full_name"}

    repo_id = find_repo_id(full_name)
    if repo_id is None:
        return {"accepted": False, "reason": f"{full_name} 还没有连接"}

    if x_github_event not in SUPPORTED_EVENTS:
        return {"accepted": True, "event": x_github_event, "repo_id": str(repo_id), "action": "ignored"}

    # 放到后台执行：GitHub 要求 10 秒内回复，而一次同步可能要几十秒
    background_tasks.add_task(sync_repository_by_id, repo_id, RepoSyncRequest(limit=settings.auto_sync_limit))
    return {"accepted": True, "event": x_github_event, "repo_id": str(repo_id), "action": "sync_scheduled"}
