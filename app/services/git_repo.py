"""6.5 第 1 件事：认出仓库身份，把代码拿到本地（Checkout）。

两种模式：
- managed：系统自己 git clone 到 .data/repos/ 下，系统负责更新
- local：用户自己的文件夹，系统只读，绝不 clone / pull / checkout
"""

import base64
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException

from app.core.config import settings
from app.db.models import Repository

logger = logging.getLogger(__name__)

GITHUB_HOSTS = {"github.com", "www.github.com"}


@dataclass(frozen=True)
class GitRemoteInfo:
    owner: str
    repo: str
    clone_url: str
    host: str | None
    default_branch: str | None = None


# ---------------------------------------------------------------- 认身份


def parse_git_remote_url(remote_url: str) -> GitRemoteInfo:
    """把远程地址拆成 host / owner / repo。支持两种写法：

    https://github.com/owner/repo.git     （HTTPS）
    git@github.com:owner/repo.git         （SSH，用密钥登录的人常用）
    """
    value = remote_url.strip()
    scp_like = re.match(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$", value)
    if scp_like and "://" not in value:
        host, path = scp_like.group("host"), scp_like.group("path")
    else:
        parsed = urlparse(value)
        host, path = parsed.hostname, parsed.path

    parts = [p for p in path.replace("\\", "/").strip("/").split("/") if p]
    if not host or len(parts) < 2:
        raise HTTPException(status_code=400, detail=f"无法从远程地址解析出 owner/repo：{remote_url}")
    owner = parts[-2]
    repo = re.sub(r"\.git$", "", parts[-1], flags=re.IGNORECASE)
    return GitRemoteInfo(owner=owner, repo=repo, clone_url=value, host=host.lower())


def infer_provider(requested: str | None, remote_host: str | None) -> str:
    """host 不是 github.com，就当作「兼容 GitHub API 的自建服务」。"""
    provider = requested or "github"
    if remote_host and remote_host not in GITHUB_HOSTS and provider == "github":
        return "github_compatible"
    return provider


def effective_api_base_url(provider: str, requested: str | None, remote_host: str | None) -> str:
    if provider == "github":
        return "https://api.github.com"
    if requested and requested.strip():
        return requested.strip().rstrip("/")
    if remote_host:
        return f"https://{remote_host}/api/v3"  # GitHub 企业版的固定 API 路径
    raise HTTPException(status_code=400, detail="github_compatible 需要提供 api_base_url")


def _git_output(args: list[str], cwd: Path) -> str:
    """执行一条只读的 git 查询命令，返回输出；失败就转成 400 错误。"""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, timeout=30, env=git_env(None)
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail="找不到 git 程序，请先安装 Git") from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=400, detail=f"检查本地仓库失败：{(exc.stderr or str(exc)).strip()}") from exc
    return result.stdout.strip()


def inspect_local_repository(raw_path: str) -> tuple[Path, GitRemoteInfo]:
    """local 模式：用户只给一个文件夹，系统用 4 条 git 命令查出它是哪个仓库。"""
    path = Path(os.path.expandvars(os.path.expanduser(raw_path.strip().strip('"')))).resolve()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"本地路径不存在或不是文件夹：{path}")

    # ① 是 Git 仓库吗？（普通文件夹会报错 not a git repository）
    try:
        inside = _git_output(["rev-parse", "--is-inside-work-tree"], path)
    except HTTPException:
        inside = "false"
    if inside != "true":
        raise HTTPException(status_code=400, detail=f"这个文件夹不是 Git 仓库：{path}")

    # ② 仓库根目录在哪？（用户给的可能是某个子文件夹）
    root = Path(_git_output(["rev-parse", "--show-toplevel"], path)).resolve()
    # ③ 对应的远程地址 → 拆出 owner/repo。没有 origin 就无法同步 Issue/PR/CI，拒绝
    remote = parse_git_remote_url(_git_output(["remote", "get-url", "origin"], root))
    # ④ 当前分支
    branch = _git_output(["branch", "--show-current"], root) or None
    return root, GitRemoteInfo(remote.owner, remote.repo, remote.clone_url, remote.host, branch)


# ---------------------------------------------------------------- Checkout


def git_env(token: str | None) -> dict[str, str]:
    """git 命令的环境变量。

    - GIT_TERMINAL_PROMPT=0：没权限时直接失败，而不是在后台卡住等人输入密码
    - Token 通过 GIT_CONFIG_* 环境变量「只在这一次命令里」变成请求头，
      不会写进 .git/config，也不会出现在命令行参数里（任务管理器看不到）。
      原项目把 Token 拼进 clone 地址，结果被 git 明文保存在 .git/config 里。
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
    return env


def _run_git(args: list[str], cwd: Path | None = None, token: str | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,  # 大仓库 clone 可能比较久，但不能无限等
        env=git_env(token),
    )


def _is_git_work_tree(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=str(path), capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def managed_checkout_path(repo: Repository) -> Path:
    """系统管理的代码放在 .data/repos/{owner}__{repo}/。"""
    root = Path(settings.repo_checkout_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    return (root / repo.full_name.replace("/", "__")).resolve()


def resolve_local_path(repo: Repository) -> Path:
    """仓库的代码文件夹：local 用用户给的；managed 用记录的位置，没有就用默认位置。"""
    if repo.local_path:
        return Path(repo.local_path).resolve()
    return managed_checkout_path(repo)


def sync_checkout(repo: Repository, token: str | None = None) -> Path:
    """保证本地有一份可读的代码，返回代码文件夹。"""
    if not shutil.which("git"):
        raise RuntimeError("找不到 git 程序，无法准备代码")

    target = resolve_local_path(repo)

    # ---- local：只检查，不动 ----
    if repo.checkout_mode == "local":
        if not _is_git_work_tree(target):
            raise RuntimeError(f"已配置的本地仓库路径不是 Git 仓库：{target}")
        return target

    branch = repo.default_branch or "main"

    # ---- managed，已经下载过：更新到远程最新 ----
    if (target / ".git").exists():
        try:
            _run_git(["fetch", "--depth", "1", "origin", branch], cwd=target, token=token)
            # 系统自己的副本，直接对齐到远程最新版（不会有用户的修改需要保护）
            _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=target)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # 附加步骤失败不拖垮主流程：网络断了就继续用上次下载的代码
            logger.warning("更新 %s 失败，继续使用旧代码：%s", target, getattr(exc, "stderr", exc))
        return target

    # ---- managed，第一次：clone ----
    if target.exists() and any(target.iterdir()):
        # 目标文件夹里有别的东西：拒绝，绝不删除用户的文件
        raise RuntimeError(f"下载目标已存在且不是空文件夹，也不是 Git 仓库：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    clone_url = repo.clone_url or f"https://github.com/{repo.full_name}.git"
    # --depth 1：只要最新一版，不要全部历史（快、省空间）
    _run_git(["clone", "--depth", "1", "--branch", branch, clone_url, str(target)], token=token)
    return target
