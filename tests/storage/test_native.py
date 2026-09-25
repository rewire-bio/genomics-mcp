"""Native reader isolation: scrubbed environment, deadline kill, stderr redaction."""

from __future__ import annotations

import time

import pytest
from gm_test_support import make_settings

from genomics_mcp.errors import GenomicsError
from genomics_mcp.storage.native import NativeRunner

SELF = "genomics_mcp.storage._selftest"


@pytest.fixture
def runner(tmp_path, monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "AKIADUMMYDUMMYDUMMY0",
        "AWS_SECRET_ACCESS_KEY": "dummy",
        "HTS_S3_HOST": "evil.example",
        "REF_PATH": "/tmp/ambient-ref",
        "REF_CACHE": "/tmp/ambient-cache/%s",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
        "NETRC": "/tmp/netrc",
        "CURL_HOME": "/tmp",
        "HTS_PATH": "/tmp/plugins",
        "PYTHONPATH": "/tmp/evil",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/g.json",
    }.items():
        monkeypatch.setenv(k, v)
    return NativeRunner(make_settings(tmp_path, []))


async def test_child_environment_is_scrubbed(runner):
    env = await runner.run(f"{SELF}:env_report", {}, timeout_s=20)
    keys = set(env["keys"])
    for bad in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "HTS_S3_HOST",
        "HTTPS_PROXY",
        "http_proxy",
        "NETRC",
        "CURL_HOME",
        "HTS_PATH",
        "PYTHONPATH",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        assert bad not in keys, bad
    iso = str(runner.base)
    assert env["REF_PATH"].startswith(iso) and env["REF_CACHE"].startswith(iso)
    assert env["HOME"].startswith(iso) and env["AWS_CONFIG_FILE"].startswith(iso)
    assert env["cwd"].startswith(str(runner.base.resolve())) or env["cwd"].startswith(iso)
    assert env["cwd_entries"] == []
    assert "AWS_EC2_METADATA_DISABLED" in keys


async def test_deadline_kills_the_child(runner):
    started = time.monotonic()
    with pytest.raises(GenomicsError) as exc:
        await runner.run(f"{SELF}:sleep", {"seconds": 60}, timeout_s=1.5)
    assert exc.value.info.code == "timeout"
    assert time.monotonic() - started < 5


async def test_native_stderr_is_captured_and_redacted(runner):
    signed = "https://bucket.example/x.bam?X-Amz-Signature=abcdef123456&X-Amz-Credential=AKIAXX"
    with pytest.raises(GenomicsError) as exc:
        await runner.run(f"{SELF}:fail_with_stderr", {"text": signed}, timeout_s=20)
    assert exc.value.info.code == "invalid_input"
    stderr = exc.value.info.details["native_stderr"]
    assert "native message about" in stderr
    assert "abcdef123456" not in stderr


async def test_only_listed_tasks_run(runner):
    for task in ("os:system", "genomics_mcp.storage._selftest:main", "genomics_mcp.nope:x"):
        with pytest.raises(GenomicsError) as exc:
            await runner.run(task, {}, timeout_s=20)
        assert exc.value.info.code == "internal_error"
