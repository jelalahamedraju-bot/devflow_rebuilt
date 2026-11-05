"""6.5 第 2 件事：Project Preflight（项目预扫描）。

只读「说明书」：README、docs/、依赖清单；跳过 .git / node_modules；拦截 .env 和私钥。
生成项目文档记录 + 扫描状态，不生成向量、不碰 Milvus。
"""

import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db.models import Document, ProjectIndexState
from app.services import project_indexing as pi
from app.tests.conftest import make_repo
from app.tests.gitutil import commit_file, make_git_repo

PROJECT_FILES = {
    "README.md": "# Demo Shop\n\n一个演示用的电商后端，FastAPI + PostgreSQL。\n",
    "ARCHITECTURE.md": "# 架构\n\n分为 api / services / db 三层。\n",
    "CHANGELOG.md": "# 1.0\n- 第一版\n",
    "docs/adr/001-use-postgres.md": "# ADR 001\n选择 PostgreSQL 因为需要事务。\n",
    "pyproject.toml": '[project]\nname = "demo-shop"\ndependencies = ["fastapi"]\n',
    "frontend/package.json": '{"name": "web"}',
    "app/main.py": "print('源码不进预扫描')\n",  # 普通源码：不读
    "notes.md": "顶层随手记，不是权威文档",  # 不在白名单的 md：不读
    ".env": "GITHUB_TOKEN=github_pat_LEAK",  # 密钥：必须拦截
    "deploy/server.pem": "-----BEGIN PRIVATE KEY-----",  # 私钥：必须拦截
    "node_modules/lib/README.md": "# 第三方库的说明",  # 依赖目录：跳过
    ".venv/README.md": "# 虚拟环境",  # 跳过
}


@pytest.fixture
def project(tmp_path):
    return make_git_repo(tmp_path / "shop", PROJECT_FILES, origin="https://github.com/o/shop.git")


def local_repo(db, path):
    repo = make_repo(db, "o", "shop")
    repo.checkout_mode = "local"
    repo.local_path = str(path)
    db.commit()
    return repo


# ---------- 挑文件 ----------


def test_discover_picks_docs_and_manifests_only(project):
    found = {c.relative_path: (c.source_type, c.tier) for c in pi.discover_project_docs(project)}

    assert found == {
        "README.md": ("project_doc", "authoritative"),
        "ARCHITECTURE.md": ("project_doc", "authoritative"),
        "docs/adr/001-use-postgres.md": ("project_doc", "authoritative"),
        "CHANGELOG.md": ("project_doc", "soft_clue"),
        "pyproject.toml": ("project_manifest", "derived"),
        "frontend/package.json": ("project_manifest", "derived"),
    }


# ---------- 指纹 ----------


def test_fingerprint_is_stable_and_changes_when_docs_change(project):
    first = pi.project_fingerprint(project)
    assert pi.project_fingerprint(project) == first  # 什么都没变：指纹不变

    time.sleep(0.01)
    commit_file(project, "README.md", "# Demo Shop v2\n")
    assert pi.project_fingerprint(project) != first  # 文档改了：指纹变了


# ---------- 扫描 ----------


def test_scan_writes_three_kinds_of_documents_and_becomes_ready(db, project, monkeypatch):
    repo = local_repo(db, project)

    state = pi.scan_project_index(db, repo)

    assert state["status"] == "ready"
    assert state["docs_indexed"] == 7  # 1 份概况 + 4 份文档 + 2 份清单
    assert "Python" in state["summary"]["tech_stack"]
    assert "JavaScript/Node.js" in state["summary"]["tech_stack"]

    docs = db.query(Document).filter(Document.repo_id == repo.id).all()
    kinds = sorted(d.source_type for d in docs)
    assert kinds == ["project_doc"] * 4 + ["project_manifest"] * 2 + ["project_overview"]

    readme = next(d for d in docs if d.meta.get("path") == "README.md")
    assert "演示用的电商后端" in readme.content
    assert readme.meta["tier"] == "authoritative"

    everything = " ".join(d.content for d in docs)
    assert "github_pat_LEAK" not in everything  # .env 没有被读进来
    assert "PRIVATE KEY" not in everything
    assert "源码不进预扫描" not in everything


def test_rescan_replaces_instead_of_duplicating(db, project):
    repo = local_repo(db, project)
    pi.scan_project_index(db, repo)
    pi.scan_project_index(db, repo)

    assert db.query(Document).filter(Document.repo_id == repo.id).count() == 7


# ---------- 状态 ----------


def test_state_lifecycle_missing_ready_stale(db, project):
    repo = local_repo(db, project)
    assert pi.get_project_index_state(db, repo)["status"] == "missing"  # 还没扫过

    pi.scan_project_index(db, repo)
    assert pi.get_project_index_state(db, repo)["status"] == "ready"

    time.sleep(0.01)
    commit_file(project, "docs/adr/001-use-postgres.md", "# ADR 001\n改成了 MySQL\n")
    assert pi.get_project_index_state(db, repo)["status"] == "stale"  # 扫过，但文档已经变了


def test_scan_failure_is_recorded_with_reason(db, tmp_path):
    repo = local_repo(db, tmp_path / "does-not-exist")

    state = pi.scan_project_index(db, repo)

    assert state["status"] == "failed"
    assert state["error_message"]  # 告诉用户失败在哪，而不是一直转圈
    assert db.query(Document).filter(Document.repo_id == repo.id).count() == 0


def test_scan_does_not_touch_vector_store(db, project, monkeypatch):
    """和 6.4 一样：预扫描只登记资料，不生成向量。没有 Milvus 也能 ready。"""
    import builtins

    real_import = builtins.__import__

    def no_vector_libs(name, *args, **kwargs):
        if name.split(".")[0] in {"pymilvus", "sentence_transformers"}:
            raise AssertionError("预扫描不应该用到向量库")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_vector_libs)
    repo = local_repo(db, project)

    assert pi.scan_project_index(db, repo)["status"] == "ready"


def test_snooze_sets_a_future_time(db, project):
    repo = local_repo(db, project)
    state = pi.snooze_project_index(db, repo, days=7)

    assert state["status"] == "missing"  # 暂缓只是「先别提醒我」，不是第六种状态
    assert state["snoozed_until"] is not None


# ---------- API ----------


@pytest.fixture
def api(db, monkeypatch):
    from app.api.routes import project_index

    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(pi, "SessionLocal", factory, raising=False)
    monkeypatch.setattr(pi, "sync_checkout", lambda repo, token=None: pi.resolve_local_path(repo))

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    from app.db.session import get_db

    app = FastAPI()
    app.include_router(project_index.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_api_scan_runs_in_background_and_reports_ready(db, project, api):
    repo = local_repo(db, project)
    url = f"/api/repos/{repo.id}/memory-index"

    assert api.get(f"{url}/state").json()["status"] == "missing"

    resp = api.post(f"{url}/scan")
    assert resp.status_code == 202  # 已接受，后台执行

    # TestClient 会在返回前把后台任务跑完
    state = api.get(f"{url}/state").json()
    assert state["status"] == "ready"
    assert state["docs_indexed"] == 7
    assert db.query(ProjectIndexState).count() == 1


def test_api_snooze_and_unknown_repo(db, project, api):
    repo = local_repo(db, project)
    resp = api.post(f"/api/repos/{repo.id}/memory-index/snooze", json={"days": 3})
    assert resp.status_code == 200
    assert resp.json()["snoozed_until"]

    import uuid

    assert api.get(f"/api/repos/{uuid.uuid4()}/memory-index/state").status_code == 404


def test_symlink_or_path_escape_is_not_followed(db, tmp_path):
    """扫描只在项目文件夹内部进行：路径不能「溜出去」读到外面的文件。"""
    outside = tmp_path / "secret_dir"
    outside.mkdir()
    (outside / "README.md").write_text("外面的机密")
    proj = make_git_repo(tmp_path / "p", {"README.md": "# p"}, origin="https://github.com/o/p.git")
    try:
        os.symlink(outside, proj / "docs", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前系统不允许创建符号链接")

    names = [c.relative_path for c in pi.discover_project_docs(proj)]
    assert names == ["README.md"]
