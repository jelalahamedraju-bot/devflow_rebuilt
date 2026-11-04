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
