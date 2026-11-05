"""测试用的小工具：在临时文件夹里造一个真实的 Git 仓库（不联网）。"""

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    # -c 设置提交人信息：GitHub Actions 的临时电脑上没有配置 git 用户
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_git_repo(path: Path, files: dict[str, str], origin: str | None = None) -> Path:
    """建一个有一次提交的仓库。files: {"相对路径": "内容"}"""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def commit_file(repo: Path, rel: str, content: str) -> None:
    (repo / rel).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"update {rel}")
