"""6.4 Issue 同步链路的测试。

不联网：用 monkeypatch 把「去 GitHub 拉 Issue」换成返回固定数据的假函数；
数据库由 conftest.py 里的 db fixture 提供（默认 SQLite，可切 PostgreSQL）。
"""

from app.db.models import Issue
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository
from app.tests.conftest import make_repo


def github_issue(gid=6078323038, number=78, state="open", title="Error: Could not import 'app'."):
    """模拟 GitHub API 返回的一条 Issue JSON（只保留用得到的字段）。

    gid 用真实量级：GitHub 的 ID 早已超过 32 位整数上限（约 21 亿）。
    """
    return {
        "id": gid,
        "number": number,
        "title": title,
        "body": "Realistic issue body",
        "state": state,
        "labels": [{"name": "bug"}],
        "user": {"login": "contributor"},
        "assignees": [{"login": "alice"}],
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "closed_at": None,
    }


def fake_github(monkeypatch, items_by_repo):
    """把 list_issues 换成假函数：按 owner/repo 返回预先准备好的数据。"""

    async def fake_list_issues(owner, repo, **_kwargs):
        return items_by_repo[f"{owner}/{repo}"]

    async def no_pull_requests(*_args, **_kwargs):
        return []

    monkeypatch.setattr("app.services.repos_sync.list_issues", fake_list_issues)
    # 这组测试只关心 Issue，PR 一律返回空列表，避免真的去请求 GitHub
    monkeypatch.setattr("app.services.repos_sync.list_pull_requests", no_pull_requests)


# 1. 基本功能：GitHub 的 Issue 能存进数据库，字段转换正确
async def test_sync_saves_issue_to_database(db, monkeypatch):
    repo = make_repo(db)
    fake_github(monkeypatch, {repo.full_name: [github_issue()]})

    result = await sync_repository(db, repo, RepoSyncRequest(limit=5))

    issue = db.query(Issue).filter(Issue.repo_id == repo.id).one()
    assert result.status == "completed"
    assert result.synced == {"issues": 1, "pull_requests": 0}
    assert issue.number == 78
    assert issue.github_issue_id == 6078323038
    assert issue.labels == ["bug"]
    assert issue.author == "contributor"
    assert issue.assignees == ["alice"]
    assert issue.updated_at.year == 2024
    assert repo.last_sync_at is not None


# 2. 幂等：同一个 Issue 第二次同步时从 open 变 closed，应更新原行而不是新增一行
async def test_resync_updates_existing_row(db, monkeypatch):
    repo = make_repo(db)

    fake_github(monkeypatch, {repo.full_name: [github_issue(state="open")]})
    await sync_repository(db, repo, RepoSyncRequest())
    fake_github(monkeypatch, {repo.full_name: [github_issue(state="closed")]})
    await sync_repository(db, repo, RepoSyncRequest())

    rows = db.query(Issue).filter(Issue.repo_id == repo.id).all()
    assert len(rows) == 1
    assert rows[0].state == "closed"


# 3. 仓库隔离：两个仓库都有 #42，各存各的，互不覆盖
async def test_two_repos_with_same_issue_number_do_not_mix(db, monkeypatch):
    repo_a = make_repo(db, "team", "repo-a")
    repo_b = make_repo(db, "team", "repo-b")
    fake_github(
        monkeypatch,
        {
            "team/repo-a": [github_issue(gid=1, number=42, title="A 的 #42")],
            "team/repo-b": [github_issue(gid=2, number=42, title="B 的 #42")],
        },
    )

    await sync_repository(db, repo_a, RepoSyncRequest())
    await sync_repository(db, repo_b, RepoSyncRequest())

    a = db.query(Issue).filter(Issue.repo_id == repo_a.id, Issue.number == 42).one()
    b = db.query(Issue).filter(Issue.repo_id == repo_b.id, Issue.number == 42).one()
    assert a.title == "A 的 #42"
    assert b.title == "B 的 #42"


# 4. GitHub 的 issues 接口会混进 PR（带 pull_request 字段），list_issues 必须过滤掉
async def test_list_issues_filters_out_pull_requests(monkeypatch):
    from app.services.github import issues as issues_module

    async def fake_get(self, path, params=None):
        return [
            {"id": 1, "number": 1, "title": "真 Issue"},
            {"id": 2, "number": 2, "title": "其实是 PR", "pull_request": {"url": "..."}},
        ]

    monkeypatch.setattr("app.services.github.client.GitHubClient.get", fake_get)

    items = await issues_module.list_issues("o", "r", limit=10)
    assert [i["number"] for i in items] == [1]
