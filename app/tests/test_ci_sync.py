"""6.4 CI（GitHub Actions）同步的测试。

一次 Workflow Run = 一次 CI 运行（比如 push 后自动跑的测试）。
- Run 本体：按 repo_id + GitHub run ID 有则更新、无则新增
- jobs：每个 Job 和它的 Steps，整理成 JSON 存在 Run 上
- 日志：只有 conclusion == "failure" 的运行才下载（成功日志量大、价值低）
- 日志下载失败不能拖垮整次同步（GitHub 下载日志需要 Token，没配就会 403）
"""

from app.db.models import WorkflowRun
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository
from app.tests.conftest import make_repo


def github_run(gid=19000000001, name="CI", status="completed", conclusion="success"):
    return {
        "id": gid,
        "name": name,
        "status": status,  # queued / in_progress / completed
        "conclusion": conclusion,  # success / failure / cancelled ...（没跑完时为 None）
        "html_url": f"https://github.com/o/r/actions/runs/{gid}",
        "created_at": "2024-03-01T00:00:00Z",
        "updated_at": "2024-03-01T00:05:00Z",
    }


def github_job(name="test", conclusion="failure"):
    return {
        "id": 5500000001,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "html_url": "https://github.com/o/r/actions/runs/1/job/2",
        "started_at": "2024-03-01T00:00:10Z",
        "completed_at": "2024-03-01T00:04:00Z",
        "steps": [
            {"name": "Checkout", "status": "completed", "conclusion": "success", "number": 1},
            {"name": "Run pytest", "status": "completed", "conclusion": conclusion, "number": 2},
        ],
        "runner_name": "GitHub Actions 2",  # 用不到的字段，不应该被存下来
    }


def fake_ci(monkeypatch, runs, jobs_by_run=None, logs_by_run=None):
    jobs_by_run = jobs_by_run or {}
    logs_by_run = logs_by_run or {}
    log_calls = []

    async def fake_runs(owner, repo, **_kwargs):
        return runs

    async def fake_jobs(owner, repo, run_id, **_kwargs):
        return jobs_by_run.get(run_id, [])

    async def fake_logs(owner, repo, run_id, **_kwargs):
        log_calls.append(run_id)
        result = logs_by_run.get(run_id, "")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("app.services.repos_sync.list_workflow_runs", fake_runs)
    monkeypatch.setattr("app.services.repos_sync.list_workflow_run_jobs", fake_jobs)
    monkeypatch.setattr("app.services.repos_sync.get_workflow_run_logs", fake_logs)
    return log_calls


# 8. CI 运行能存下来；jobs 只保留需要的字段；只有失败的运行才去下载日志
async def test_sync_saves_runs_and_fetches_logs_only_for_failures(db, monkeypatch):
    repo = make_repo(db)
    ok = github_run(gid=1, name="lint", conclusion="success")
    bad = github_run(gid=2, name="tests", conclusion="failure")
    log_calls = fake_ci(
        monkeypatch,
        runs=[ok, bad],
        jobs_by_run={2: [github_job("pytest", "failure")]},
        logs_by_run={2: "FAILED tests/test_app.py::test_import - ModuleNotFoundError"},
    )

    result = await sync_repository(db, repo, RepoSyncRequest())

    assert result.synced["workflow_runs"] == 2
    assert log_calls == [2]  # 成功的 run 1 没有去下载日志

    failed = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id, WorkflowRun.github_run_id == 2).one()
    assert failed.conclusion == "failure"
    assert "ModuleNotFoundError" in failed.logs_text
    assert failed.jobs[0]["name"] == "pytest"
    assert [s["name"] for s in failed.jobs[0]["steps"]] == ["Checkout", "Run pytest"]
    assert "runner_name" not in failed.jobs[0]

    passed = db.query(WorkflowRun).filter(WorkflowRun.github_run_id == 1).one()
    assert passed.logs_text is None


# 9. 同一次运行从「进行中」变成「失败」：更新原行，并在失败后补上日志
async def test_resync_updates_run_from_in_progress_to_failure(db, monkeypatch):
    repo = make_repo(db)
    fake_ci(monkeypatch, runs=[github_run(gid=7, status="in_progress", conclusion=None)])
    await sync_repository(db, repo, RepoSyncRequest())

    fake_ci(
        monkeypatch,
        runs=[github_run(gid=7, status="completed", conclusion="failure")],
        logs_by_run={7: "Error: exit code 1"},
    )
    await sync_repository(db, repo, RepoSyncRequest())

    runs = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).all()
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].conclusion == "failure"
    assert runs[0].logs_text == "Error: exit code 1"


# 10. 日志下载失败（比如没配 Token 被 GitHub 拒绝）不能让整次同步失败
async def test_log_download_error_does_not_break_sync(db, monkeypatch):
    repo = make_repo(db)
    fake_ci(
        monkeypatch,
        runs=[github_run(gid=3, conclusion="failure")],
        logs_by_run={3: PermissionError("403: Must have admin rights / token required")},
    )

    result = await sync_repository(db, repo, RepoSyncRequest())

    assert result.status == "completed"
    run = db.query(WorkflowRun).filter(WorkflowRun.github_run_id == 3).one()
    assert run.conclusion == "failure"  # 运行记录照样存下来了
    assert run.logs_text is None  # 只是没有日志


# 11. 关掉 sync_workflow_runs 开关时不处理 CI
async def test_ci_switch_off(db, monkeypatch):
    repo = make_repo(db)
    fake_ci(monkeypatch, runs=[github_run()])

    result = await sync_repository(db, repo, RepoSyncRequest(sync_workflow_runs=False))

    assert "workflow_runs" not in result.synced
    assert db.query(WorkflowRun).count() == 0
