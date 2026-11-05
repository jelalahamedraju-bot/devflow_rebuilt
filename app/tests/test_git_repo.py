"""6.5 第 1 件事：认出仓库身份 + 把代码拿到本地（Checkout）。

用真实的 git 命令、在临时文件夹里造仓库来测，不访问 GitHub。
"""

import shutil

import pytest
from fastapi import HTTPException

from app.db.models import Repository
from app.services import git_repo
from app.tests.gitutil import commit_file, git, make_git_repo

# ---------- 解析远程地址 ----------


@pytest.mark.parametrize(
    "url, host, owner, repo",
    [
        ("https://github.com/jelalahamedraju-bot/devflow_rebuilt.git", "github.com", "jelalahamedraju-bot", "devflow_rebuilt"),
        ("https://github.com/openai/openai-quickstart-python", "github.com", "openai", "openai-quickstart-python"),
        ("git@github.com:encode/httpx.git", "github.com", "encode", "httpx"),  # SSH 写法
        ("https://git.mycompany.com/team/payments.git", "git.mycompany.com", "team", "payments"),
    ],
)
def test_parse_git_remote_url(url, host, owner, repo):
    info = git_repo.parse_git_remote_url(url)
    assert (info.host, info.owner, info.repo) == (host, owner, repo)


def test_parse_git_remote_url_rejects_garbage():
    with pytest.raises(HTTPException):
        git_repo.parse_git_remote_url("not-a-repo")


# ---------- 认平台 ----------


def test_github_host_is_github_with_public_api():
    assert git_repo.infer_provider(None, "github.com") == "github"
    assert git_repo.effective_api_base_url("github", None, "github.com") == "https://api.github.com"


def test_other_host_is_github_compatible_with_enterprise_style_api():
    assert git_repo.infer_provider(None, "git.mycompany.com") == "github_compatible"
    assert git_repo.effective_api_base_url("github_compatible", None, "git.mycompany.com") == "https://git.mycompany.com/api/v3"
    # 用户明确给了 API 地址，就用用户给的
    assert git_repo.effective_api_base_url("github_compatible", "https://x.example/api/", "git.mycompany.com") == "https://x.example/api"


# ---------- local 模式：4 条 git 命令 ----------


def test_inspect_local_repository_from_a_subfolder(tmp_path):
    root = make_git_repo(
        tmp_path / "proj",
        {"README.md": "# demo", "app/services/x.py": "pass"},
        origin="https://github.com/jelalahamedraju-bot/devflow_rebuilt.git",
    )

    path, info = git_repo.inspect_local_repository(str(root / "app" / "services"))  # 给的是子文件夹

    assert path == root.resolve()  # ② 找到了根目录
    assert (info.owner, info.repo) == ("jelalahamedraju-bot", "devflow_rebuilt")  # ③ 从 origin 认出身份
    assert info.default_branch == "main"  # ④ 当前分支


def test_inspect_plain_folder_is_rejected(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(HTTPException) as exc:
        git_repo.inspect_local_repository(str(tmp_path / "plain"))  # ① 不是 Git 仓库
    assert exc.value.status_code == 400


def test_inspect_repo_without_origin_is_rejected(tmp_path):
    root = make_git_repo(tmp_path / "noorigin", {"a.txt": "a"})
    with pytest.raises(HTTPException):
        git_repo.inspect_local_repository(str(root))


# ---------- Checkout ----------


def make_repo_row(**kw) -> Repository:
    defaults = dict(owner="o", name="r", full_name="o/r", default_branch="main", checkout_mode="managed")
    return Repository(**{**defaults, **kw})


def test_managed_checkout_clones_then_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(git_repo.settings, "repo_checkout_dir", str(tmp_path / "checkouts"))
    upstream = make_git_repo(tmp_path / "upstream", {"README.md": "v1"})
    repo = make_repo_row(clone_url=upstream.as_uri())

    target = git_repo.sync_checkout(repo)  # 第一次：clone
    assert (target / "README.md").read_text() == "v1"
    assert git(target, "rev-list", "--count", "HEAD") == "1"  # --depth 1：只有最新一版

    commit_file(upstream, "README.md", "v2")  # 远程有了新提交
    target2 = git_repo.sync_checkout(repo)  # 第二次：更新
    assert target2 == target
    assert (target / "README.md").read_text() == "v2"


def test_managed_checkout_update_failure_keeps_old_code(tmp_path, monkeypatch):
    """网络断了 / 远程没了：更新失败就继续用旧代码，不报错。"""
    monkeypatch.setattr(git_repo.settings, "repo_checkout_dir", str(tmp_path / "checkouts"))
    upstream = make_git_repo(tmp_path / "upstream", {"README.md": "v1"})
    repo = make_repo_row(clone_url=upstream.as_uri())
    target = git_repo.sync_checkout(repo)

    shutil.rmtree(upstream, onerror=lambda f, p, e: (__import__("os").chmod(p, 0o700), f(p)))  # 远程消失

    assert git_repo.sync_checkout(repo) == target
    assert (target / "README.md").read_text() == "v1"


def test_local_checkout_is_read_only(tmp_path):
    """local 模式：系统绝不改动用户的文件夹（不 pull、不切分支、不动未提交的修改）。"""
    root = make_git_repo(tmp_path / "mine", {"README.md": "mine"}, origin="https://github.com/o/r.git")
    (root / "README.md").write_text("我写了一半还没提交", encoding="utf-8")
    head_before = git(root, "rev-parse", "HEAD")

    repo = make_repo_row(checkout_mode="local", local_path=str(root))
    assert git_repo.sync_checkout(repo) == root.resolve()

    assert (root / "README.md").read_text(encoding="utf-8") == "我写了一半还没提交"
    assert git(root, "rev-parse", "HEAD") == head_before


def test_local_checkout_that_is_no_longer_a_git_repo_fails(tmp_path):
    (tmp_path / "gone").mkdir()
    repo = make_repo_row(checkout_mode="local", local_path=str(tmp_path / "gone"))
    with pytest.raises(RuntimeError):
        git_repo.sync_checkout(repo)


def test_managed_checkout_refuses_non_empty_non_git_folder(tmp_path):
    """下载目标里已经有别的文件：拒绝，不能删用户的东西。"""
    target = tmp_path / "busy"
    target.mkdir()
    (target / "important.docx").write_text("别删我")
    upstream = make_git_repo(tmp_path / "upstream", {"README.md": "v1"})
    repo = make_repo_row(clone_url=upstream.as_uri(), local_path=str(target))

    with pytest.raises(RuntimeError):
        git_repo.sync_checkout(repo)
    assert (target / "important.docx").exists()


def test_token_is_not_written_to_git_config(tmp_path, monkeypatch):
    """原项目把 Token 拼进 clone 地址，会被明文写进 .git/config。我们改成只在这一次命令里带上。"""
    monkeypatch.setattr(git_repo.settings, "repo_checkout_dir", str(tmp_path / "checkouts"))
    upstream = make_git_repo(tmp_path / "upstream", {"README.md": "v1"})
    repo = make_repo_row(clone_url=upstream.as_uri())

    target = git_repo.sync_checkout(repo, token="github_pat_SECRET123")

    config = (target / ".git" / "config").read_text(encoding="utf-8")
    assert "SECRET123" not in config


def test_git_env_carries_token_only_as_one_off_header():
    env = git_repo.git_env("github_pat_SECRET123")
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # 没权限时直接失败，不卡在等输入密码
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "SECRET123" not in env["GIT_CONFIG_VALUE_0"]  # 是 base64 编码后的
    assert "GIT_CONFIG_KEY_0" not in git_repo.git_env(None)
