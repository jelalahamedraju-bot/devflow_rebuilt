"""6.5 第 2 件事：Project Preflight（项目预扫描）—— 读一遍项目的「说明书」。

只挑能说明项目的文件，生成 3 类 Document：
- project_overview：项目概况（名字、技术栈、目录、扫了哪些文件），可以直接放进 AI 的上下文
- project_doc：README、架构文档、ADR……第 07 章会切片进 RAG
- project_manifest：依赖清单，用来判断技术栈

这里只「登记」资料，不生成向量、不碰 Milvus（和 6.4 同一个原则）。
"""

import asyncio
import hashlib
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Document, ProjectIndexState, Repository
from app.db.session import SessionLocal
from app.services.git_repo import resolve_local_path, sync_checkout

PROJECT_INDEX_SOURCE_TYPES = ["project_overview", "project_doc", "project_manifest"]

# 顶层这几个名字的 .md 是权威说明；docs/ 下的 .md 也是
AUTHORITATIVE_TOP_LEVEL = {"README", "ARCHITECTURE", "CONTRIBUTING"}
# 顶层这几个是辅助线索（更新记录之类）
SOFT_CLUE_TOP_LEVEL = {"CHANGELOG", "HISTORY", "RELEASES"}
# 依赖 / 构建清单：从这里推断技术栈
MANIFEST_FILES = {
    "package.json", "requirements.txt", "pyproject.toml", "pnpm-workspace.yaml",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json",
}  # fmt: skip
# 这些目录不进去：版本库内部、第三方依赖、构建产物、缓存
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "dist", "build", "coverage",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt", ".data",
}  # fmt: skip
# 密钥类文件：即使名字碰巧符合规则，也绝不读
SECRET_NAMES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc", "credentials.json"}
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}

MAX_FILES = 500  # 最多看多少个文件就停（防止超大仓库扫半天）
MAX_BYTES = 400_000  # 单个文件超过这个大小就跳过


@dataclass(frozen=True)
class DocCandidate:
    path: Path
    relative_path: str
    source_type: str  # project_doc / project_manifest
    tier: str  # authoritative 权威说明 / soft_clue 辅助线索 / derived 推导出来的


# ---------------------------------------------------------------- 挑文件


def _is_secret(path: Path) -> bool:
    return path.name in SECRET_NAMES or path.name.startswith(".env") or path.suffix.lower() in SECRET_SUFFIXES


def _classify(path: Path, rel: str) -> tuple[str, str] | None:
    if _is_secret(path):
        return None
    if path.suffix.lower() == ".md":
        top_level = "/" not in rel
        if rel.startswith("docs/") or (top_level and path.stem.upper() in AUTHORITATIVE_TOP_LEVEL):
            return "project_doc", "authoritative"
        if top_level and path.stem.upper() in SOFT_CLUE_TOP_LEVEL:
            return "project_doc", "soft_clue"
        return None
    if path.name in MANIFEST_FILES:
        return "project_manifest", "derived"
    return None  # 普通源码不进预扫描：源码走「实时搜索」路线


def discover_project_docs(root: Path) -> list[DocCandidate]:
    root = root.resolve()
    found: list[DocCandidate] = []
    seen = 0
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda p: p.name):
            # 不跟随符号链接：防止通过链接「溜出」项目文件夹，读到外面的文件
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRS and not (entry.name.startswith(".") and entry.name != ".github"):
                    stack.append(entry)
                continue
            seen += 1
            if seen > MAX_FILES:
                return sorted(found, key=lambda c: c.relative_path)
            rel = entry.relative_to(root).as_posix()
            kind = _classify(entry, rel)
            if kind:
                found.append(DocCandidate(entry, rel, *kind))
    return sorted(found, key=lambda c: c.relative_path)


# ---------------------------------------------------------------- 指纹


def _git_head(root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else "no-git-head"


def project_fingerprint(root: Path) -> str:
    """项目「说明书」的指纹：Git 版本 + 每个候选文件的路径、修改时间、大小。

    只要有一份文档被改、被加、被删，指纹就会变 → 状态变成 stale（过期）。
    注意：用的是修改时间和大小，没有读文件内容算哈希（快，但理论上可能漏掉极少数情况）。
    """
    hasher = hashlib.sha256(_git_head(root).encode())
    for c in discover_project_docs(root):
        stat = c.path.stat()
        hasher.update(f"{c.relative_path}|{stat.st_mtime_ns}|{stat.st_size}".encode())
    return hasher.hexdigest()


# ---------------------------------------------------------------- 状态


def _aware(value: datetime | None) -> datetime | None:
    # SQLite 读回来的时间没有时区，统一当作 UTC
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def _state_row(db: Session, repo_id: uuid.UUID) -> ProjectIndexState | None:
    return db.query(ProjectIndexState).filter(ProjectIndexState.repo_id == repo_id).one_or_none()


def _get_or_create_row(db: Session, repo_id: uuid.UUID) -> ProjectIndexState:
    row = _state_row(db, repo_id)
    if row is None:
        row = ProjectIndexState(repo_id=repo_id, status="missing", fingerprint="", docs_indexed=0)
        db.add(row)
        db.flush()
    return row


def _payload(repo: Repository, row: ProjectIndexState | None) -> dict[str, Any]:
    status = row.status if row else "missing"
    # ready 只表示「上次扫描时」是最新的；现在指纹对不上 → stale（过期），需要重扫
    if status == "ready":
        root = resolve_local_path(repo)
        if not root.is_dir() or project_fingerprint(root) != row.fingerprint:
            status = "stale"
    return {
        "repo_id": str(repo.id),
        "status": status,
        "docs_indexed": row.docs_indexed if row else 0,
        "last_scan_at": _aware(row.last_scan_at) if row else None,
        "snoozed_until": _aware(row.snoozed_until) if row else None,
        "error_message": row.error_message if row else None,
        "summary": (row.summary_json or {}) if row else {},
    }


def get_project_index_state(db: Session, repo: Repository) -> dict[str, Any]:
    return _payload(repo, _state_row(db, repo.id))


def mark_building(db: Session, repo: Repository) -> dict[str, Any]:
    row = _get_or_create_row(db, repo.id)
    row.status = "building"
    row.error_message = None
    db.commit()
    return _payload(repo, row)


def snooze_project_index(db: Session, repo: Repository, days: int = 7) -> dict[str, Any]:
    """「先别提醒我」：只记一个时间，不改状态、不删资料。"""
    row = _get_or_create_row(db, repo.id)
    row.snoozed_until = datetime.now(UTC) + timedelta(days=days)
    db.commit()
    return _payload(repo, row)


# ---------------------------------------------------------------- 扫描


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_BYTES or b"\x00" in data[:4096]:  # 太大或者是二进制文件
        return None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()[:200] or fallback
    return fallback


def _tech_stack(candidates: list[DocCandidate], root: Path) -> list[str]:
    names = {c.path.name for c in candidates}
    stack = []
    if names & {"pyproject.toml", "requirements.txt"}:
        stack.append("Python")
    if "package.json" in names:
        stack.append("JavaScript/Node.js")
    if "go.mod" in names:
        stack.append("Go")
    if "Cargo.toml" in names:
        stack.append("Rust")
    if names & {"pom.xml", "build.gradle"}:
        stack.append("Java")
    if (root / "Dockerfile").exists() or (root / "docker-compose.yml").exists():
        stack.append("Docker")
    return stack


def _top_dirs(root: Path) -> list[str]:
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith(".")
    )[:15]


def scan_project_index(db: Session, repo: Repository) -> dict[str, Any]:
    """扫描一次。成功 → ready；任何一步失败 → 回滚、状态 failed 并记下原因。"""
    row = _get_or_create_row(db, repo.id)
    try:
        root = resolve_local_path(repo)
        if not root.is_dir():
            raise FileNotFoundError(f"代码文件夹不存在：{root}（managed 仓库请先完成 Checkout）")

        fingerprint = project_fingerprint(root)
        candidates = discover_project_docs(root)
        docs: list[Document] = []
        for c in candidates:
            text = _read_text(c.path)
            if not text or not text.strip():
                continue
            meta = {"path": c.relative_path, "tier": c.tier, "fingerprint": fingerprint}
            docs.append(
                Document(repo_id=repo.id, source_type=c.source_type, title=_title(text, c.relative_path), content=text, meta=meta)
            )

        summary = {
            "name": repo.full_name,
            "local_path": str(root),
            "tech_stack": _tech_stack(candidates, root),
            "top_level_dirs": _top_dirs(root),
            "documents": [c.relative_path for c in candidates],
        }
        overview = "\n".join(
            [
                f"项目：{summary['name']}",
                f"技术栈：{'、'.join(summary['tech_stack']) or '未识别'}",
                f"顶层目录：{', '.join(summary['top_level_dirs']) or '无'}",
                "项目文档：",
                *[f"- {p}" for p in summary["documents"]],
            ]
        )
        docs.insert(
            0,
            Document(
                repo_id=repo.id, source_type="project_overview", title=f"{repo.full_name} 项目概况", content=overview,
                meta={"tier": "derived", "fingerprint": fingerprint},
            ),
        )  # fmt: skip

        # 整批替换：先删掉这个仓库上一次的项目文档，再写入这一次的（和 PR 文件的「整组替换」一样）
        db.query(Document).filter(
            Document.repo_id == repo.id, Document.source_type.in_(PROJECT_INDEX_SOURCE_TYPES)
        ).delete(synchronize_session=False)
        db.add_all(docs)

        row.status = "ready"
        row.fingerprint = fingerprint
        row.docs_indexed = len(docs)
        row.summary_json = summary
        row.error_message = None
        row.last_scan_at = datetime.now(UTC)
        db.commit()  # 文档和状态在同一次提交里，要么都成功，要么都不写
    except Exception as exc:
        db.rollback()  # 半截写入的文档全部撤销
        row = _get_or_create_row(db, repo.id)
        row.status = "failed"
        row.error_message = str(exc)[:1000]
        db.commit()
    return _payload(repo, row)


def _checkout_and_scan(repo_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        repo = db.get(Repository, repo_id)
        if repo is None:
            return
        try:
            path = sync_checkout(repo, settings.github_token)  # managed：先保证代码是最新的；local：只检查
            if repo.checkout_mode == "managed" and repo.local_path != str(path):
                repo.local_path = str(path)
                db.commit()
        except Exception as exc:
            row = _get_or_create_row(db, repo.id)
            row.status, row.error_message = "failed", f"准备代码失败：{exc}"[:1000]
            db.commit()
            return
        scan_project_index(db, repo)


async def checkout_and_scan_by_id(repo_id: uuid.UUID) -> None:
    """后台任务入口：git clone 和读文件都是「阻塞」操作，放到线程里跑，不卡住整个后端。"""
    await asyncio.to_thread(_checkout_and_scan, repo_id)
