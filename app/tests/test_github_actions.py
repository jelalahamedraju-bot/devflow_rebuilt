"""GitHub Actions 日志是 zip 包：get_workflow_run_logs 要负责解压并拼成文本。"""

import io
import zipfile

from app.services.github import actions
from app.services.github.client import GitHubClient


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


async def test_logs_zip_is_unpacked_into_text(monkeypatch):
    payload = make_zip({"test/2_Run pytest.txt": "FAILED test_app\n", "test/1_Checkout.txt": "ok\n"})

    async def fake_get_bytes(self, path):
        return payload

    monkeypatch.setattr(GitHubClient, "get_bytes", fake_get_bytes, raising=False)

    text = await actions.get_workflow_run_logs("o", "r", 1)

    # 按文件名排序拼接，每段前面标出来自哪个文件
    assert text.index("1_Checkout") < text.index("2_Run pytest")
    assert "FAILED test_app" in text


async def test_logs_are_truncated(monkeypatch):
    payload = make_zip({"big.txt": "x" * 1000})

    async def fake_get_bytes(self, path):
        return payload

    monkeypatch.setattr(GitHubClient, "get_bytes", fake_get_bytes, raising=False)

    text = await actions.get_workflow_run_logs("o", "r", 1, max_chars=100)

    assert len(text) == 100


async def test_job_logs_keep_the_end_not_the_beginning(monkeypatch):
    """报错一般在日志最后面：太长时要砍掉开头、保留结尾。"""
    log = "setup line\n" * 500 + "E   AssertionError: boom\n##[error]Process completed with exit code 1.\n"

    async def fake_get_bytes(self, path):
        assert path == "/repos/o/r/actions/jobs/312/logs"
        return log.encode()

    monkeypatch.setattr(GitHubClient, "get_bytes", fake_get_bytes, raising=False)

    text = await actions.get_job_logs("o", "r", 312, max_chars=200)

    assert len(text) <= 200
    assert text.endswith("Process completed with exit code 1.\n")
    assert "AssertionError: boom" in text
    assert text.startswith("…")  # 用省略号提示前面被截掉了


async def test_short_job_logs_are_kept_whole(monkeypatch):
    async def fake_get_bytes(self, path):
        return b"short log"

    monkeypatch.setattr(GitHubClient, "get_bytes", fake_get_bytes, raising=False)

    assert await actions.get_job_logs("o", "r", 1, max_chars=200) == "short log"
