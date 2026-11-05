"""6.5：连接仓库的入口升级 —— 支持 local 文件夹、选择下载位置、自建 GitHub 兼容服务。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db.models import Repository
from app.tests.gitutil import make_git_repo


@pytest.fixture
def client(db, monkeypatch):
    from app.api.routes import repos
    from app.db.session import get_db

    factory = sessionmaker(bind=db.get_bind())
    calls = {"get_repo": [], "background": []}

    async def fake_get_repo(self, owner, name):
        calls["get_repo"].append((self.base_url, owner, name))
        return {
            "full_name": f"{owner}/{name}", "name": name, "owner": {"login": owner},
            "default_branch": "main", "clone_url": f"https://example.invalid/{owner}/{name}.git",
        }  # fmt: skip

    async def fake_checkout_and_scan(repo_id):
        calls["background"].append(repo_id)

    monkeypatch.setattr("app.services.github.client.GitHubClient.get_repo", fake_get_repo)
    monkeypatch.setattr(repos, "checkout_and_scan_by_id", fake_checkout_and_scan)

    def override_db():
        with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(repos.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    return TestClient(app), calls


def test_connect_local_folder_identifies_repo_from_origin(db, client, tmp_path):
    http, calls = client
    root = make_git_repo(
        tmp_path / "mine", {"README.md": "# mine"}, origin="https://github.com/jelalahamedraju-bot/devflow_rebuilt.git"
    )

    # 只给一个文件夹（甚至是子文件夹），不填 owner / repo
    resp = http.post("/api/repos/connect", json={"local_path": str(root)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["full_name"] == "jelalahamedraju-bot/devflow_rebuilt"
    repo = db.query(Repository).one()
    assert repo.checkout_mode == "local"
    assert repo.local_path == str(root.resolve())
    assert repo.provider == "github"
    assert calls["background"] == [repo.id]  # 连接后在后台准备代码 + 预扫描


def test_connect_by_name_is_managed_and_can_choose_parent_folder(db, client, tmp_path):
    http, calls = client

    resp = http.post(
        "/api/repos/connect", json={"owner": "openai", "repo": "openai-quickstart-python", "clone_parent_dir": str(tmp_path)}
    )

    assert resp.status_code == 200, resp.text
    repo = db.query(Repository).one()
    assert repo.checkout_mode == "managed"
    assert repo.local_path == str((tmp_path / "openai-quickstart-python").resolve())
    assert repo.clone_url.endswith("openai/openai-quickstart-python.git")
    assert repo.default_branch == "main"


def test_connect_github_compatible_uses_its_own_api(db, client):
    http, calls = client

    resp = http.post(
        "/api/repos/connect",
        json={"owner": "team", "repo": "payments", "provider": "github_compatible", "api_base_url": "https://git.corp.example/api/v3"},
    )

    assert resp.status_code == 200, resp.text
    assert calls["get_repo"][0][0] == "https://git.corp.example/api/v3"  # 确认仓库时去的是公司自己的服务
    repo = db.query(Repository).one()
    assert (repo.provider, repo.api_base_url) == ("github_compatible", "https://git.corp.example/api/v3")


def test_connect_requires_folder_or_name(client):
    http, _ = client
    assert http.post("/api/repos/connect", json={}).status_code == 400


def test_connect_rejects_plain_folder(client, tmp_path):
    http, _ = client
    (tmp_path / "plain").mkdir()
    assert http.post("/api/repos/connect", json={"local_path": str(tmp_path / "plain")}).status_code == 400


async def test_sync_uses_repository_api_base_url(db, monkeypatch):
    """同步 Issue/PR/CI 时，也要去这个仓库自己的 API 地址（自建服务就不是 api.github.com）。"""
    from app.schemas.repos import RepoSyncRequest
    from app.services.repos_sync import sync_repository
    from app.tests.conftest import make_repo

    seen = []

    async def spy(owner, repo, **kwargs):
        seen.append(kwargs.get("base_url"))
        return []

    monkeypatch.setattr("app.services.repos_sync.list_issues", spy)
    repo = make_repo(db, "team", "payments")
    repo.api_base_url = "https://git.corp.example/api/v3"
    db.commit()

    await sync_repository(db, repo, RepoSyncRequest(sync_pull_requests=False, sync_workflow_runs=False))

    assert seen == ["https://git.corp.example/api/v3"]
