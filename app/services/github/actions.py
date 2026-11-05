from io import BytesIO
from zipfile import BadZipFile, ZipFile

from app.services.github.client import GitHubClient


async def list_workflow_runs(owner: str, repo: str, token: str | None = None, base_url: str | None = None, limit: int = 30) -> list[dict]:
    """CI 运行列表。注意这个接口返回的不是数组，而是 {"total_count": ..., "workflow_runs": [...]}。"""
    client = GitHubClient(token, base_url)
    return await client.get_paginated(f"/repos/{owner}/{repo}/actions/runs", item_key="workflow_runs", limit=limit)


async def list_workflow_run_jobs(owner: str, repo: str, run_id: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    """某次运行里的 Jobs（每个 Job 里有 Steps）。同样包在 {"jobs": [...]} 里。"""
    client = GitHubClient(token, base_url)
    return await client.get_paginated(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", item_key="jobs")


async def get_job_logs(owner: str, repo: str, job_id: int, token: str | None = None, base_url: str | None = None, max_chars: int = 80_000) -> str:
    """只下载某一个 Job 的日志（纯文本，不是 zip）。

    太长时「砍掉开头、保留结尾」：报错和 exit code 基本都在日志最后面，
    开头多是安装依赖之类的准备步骤。
    """
    client = GitHubClient(token, base_url)
    text = (await client.get_bytes(f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs")).decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return "…" + text[-(max_chars - 1):]  # 开头的省略号提醒：前面还有内容被截掉了


async def get_workflow_run_logs(owner: str, repo: str, run_id: int, token: str | None = None, base_url: str | None = None, max_chars: int = 80_000) -> str:
    """下载某次运行的日志。GitHub 返回的是 zip 包（每个 Step 一个 txt），这里解压后拼成一段文本。

    max_chars：日志可能非常长，只保留前面一部分，避免数据库里塞进几十 MB 的文本。
    注意：下载日志需要 Token，没配会被 GitHub 拒绝（403）。
    """
    client = GitHubClient(token, base_url)
    payload = await client.get_bytes(f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            parts: list[str] = []
            for name in sorted(archive.namelist()):
                if name.endswith("/"):  # 跳过目录
                    continue
                text = archive.read(name).decode("utf-8", errors="replace")
                parts.append(f"### {name}\n{text}")
                if sum(len(p) for p in parts) >= max_chars:
                    break
            return "\n\n".join(parts)[:max_chars]
    except BadZipFile:
        # 万一返回的不是 zip，就当作纯文本
        return payload.decode("utf-8", errors="replace")[:max_chars]
