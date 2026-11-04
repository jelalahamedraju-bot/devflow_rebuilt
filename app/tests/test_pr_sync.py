"""6.4 PR 同步链路的测试。

PR = 本体（PullRequest）+ 两种子资源：
- PRFile：改了哪些文件、增删行数、patch（代码差异）
- PRReviewComment：针对某一行代码的审查评论

规则：PR 本体按「repo_id + GitHub ID」有则更新、无则新增；
子资源每次同步「整组替换」（先删掉这个 PR 的旧文件/评论，再写入最新的）。
"""

from app.db.models import PRFile, PRReviewComment, PullRequest
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository
from app.tests.conftest import make_repo


def github_pr(gid=2900000001, number=12, state="open", merged_at=None, title="Fix import error"):
    """模拟 GitHub /pulls 接口返回的一条 PR（只保留用得到的字段）。"""
    return {
        "id": gid,
        "number": number,
        "title": title,
        "body": "This PR fixes the import error",
        "state": state,
        "user": {"login": "bob"},
        "base": {"ref": "main"},  # 要合并进去的目标分支
        "head": {"ref": "fix/import"},  # PR 自己的分支
        "merged_at": merged_at,
        "created_at": "2024-02-01T00:00:00Z",
        "updated_at": "2024-02-02T00:00:00Z",
    }


def github_file(filename="app.py", additions=3, deletions=1):
    return {
        "filename": filename,
        "status": "modified",
        "additions": additions,
        "deletions": deletions,
        "patch": "@@ -1,2 +1,4 @@\n-import ap\n+import app",
    }


def github_review_comment(gid=7700000001, body="这里要加测试", line=10):
    return {
        "id": gid,
        "body": body,
        "path": "app.py",
        "line": line,
        "user": {"login": "carol"},
        "created_at": "2024-02-01T12:00:00Z",
        "updated_at": "2024-02-01T12:00:00Z",
    }


def fake_github(monkeypatch, prs, files_by_number=None, comments_by_number=None):
    """把 GitHub 的 PR 相关接口都换成假函数；Issue 返回空。"""
    files_by_number = files_by_number or {}
    comments_by_number = comments_by_number or {}
    calls = {"pulls": 0}

    async def no_issues(*_args, **_kwargs):
        return []

    async def fake_list_pull_requests(owner, repo, **_kwargs):
        calls["pulls"] += 1
        return prs

    async def fake_list_files(owner, repo, number, **_kwargs):
        return files_by_number.get(number, [])

    async def fake_list_comments(owner, repo, number, **_kwargs):
        return comments_by_number.get(number, [])

    monkeypatch.setattr("app.services.repos_sync.list_issues", no_issues)
    monkeypatch.setattr("app.services.repos_sync.list_pull_requests", fake_list_pull_requests)
    monkeypatch.setattr("app.services.repos_sync.list_pull_request_files", fake_list_files)
    monkeypatch.setattr("app.services.repos_sync.list_pull_request_review_comments", fake_list_comments)
    return calls


# 5. PR 本体 + 文件 + 评论 都能存下来，字段转换正确
async def test_sync_saves_pr_with_files_and_review_comments(db, monkeypatch):
    repo = make_repo(db)
    fake_github(
        monkeypatch,
        prs=[github_pr()],
        files_by_number={12: [github_file("app.py"), github_file("tests/test_app.py", additions=20, deletions=0)]},
        comments_by_number={12: [github_review_comment()]},
    )

    result = await sync_repository(db, repo, RepoSyncRequest())

    pr = db.query(PullRequest).filter(PullRequest.repo_id == repo.id).one()
    assert result.synced == {"issues": 0, "pull_requests": 1, "workflow_runs": 0}
    assert pr.number == 12
    assert pr.github_pr_id == 2900000001
    assert pr.author == "bob"
    assert pr.base_branch == "main"
    assert pr.head_branch == "fix/import"
    assert pr.merged_at is None

    files = db.query(PRFile).filter(PRFile.pr_id == pr.id).order_by(PRFile.filename).all()
    assert [(f.filename, f.additions, f.deletions) for f in files] == [
        ("app.py", 3, 1),
        ("tests/test_app.py", 20, 0),
    ]
    assert "+import app" in files[0].patch

    comment = db.query(PRReviewComment).filter(PRReviewComment.pr_id == pr.id).one()
    assert comment.body == "这里要加测试"
    assert comment.path == "app.py"
    assert comment.line == 10
    assert comment.author == "carol"


# 6. 再同步一次：PR 本体更新原行；文件列表整组替换（删掉的文件不能残留，也不能重复）
async def test_resync_updates_pr_and_replaces_files(db, monkeypatch):
    repo = make_repo(db)

    fake_github(monkeypatch, prs=[github_pr()], files_by_number={12: [github_file("a.py"), github_file("b.py")]})
    await sync_repository(db, repo, RepoSyncRequest())

    # PR 被合并了，而且作者在合并前撤掉了 b.py 的改动、新增了 c.py
    merged = github_pr(state="closed", merged_at="2024-02-03T00:00:00Z")
    fake_github(monkeypatch, prs=[merged], files_by_number={12: [github_file("a.py"), github_file("c.py")]})
    await sync_repository(db, repo, RepoSyncRequest())

    prs = db.query(PullRequest).filter(PullRequest.repo_id == repo.id).all()
    assert len(prs) == 1
    assert prs[0].state == "closed"
    assert prs[0].merged_at is not None

    names = sorted(f.filename for f in db.query(PRFile).filter(PRFile.pr_id == prs[0].id))
    assert names == ["a.py", "c.py"]


# 7. 关掉 sync_pull_requests 开关时，根本不去请求 PR 接口
async def test_pr_switch_off_skips_pr_api(db, monkeypatch):
    repo = make_repo(db)
    calls = fake_github(monkeypatch, prs=[github_pr()])

    result = await sync_repository(db, repo, RepoSyncRequest(sync_pull_requests=False))

    assert calls["pulls"] == 0
    assert "pull_requests" not in result.synced
    assert db.query(PullRequest).count() == 0
