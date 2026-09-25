"""S3: isolated botocore sessions, explicit profiles, no ambient credentials or config.

Unit tests never contact AWS. The MinIO integration tests run only when
GENOMICS_MCP_TEST_S3_KEY/SECRET are set (local MinIO on 127.0.0.1, synthetic data).
"""

from __future__ import annotations

import urllib.request

import botocore.configloader
import botocore.credentials
import pytest
from gm_test_support import (
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_KEY,
    MINIO_SECRET,
    REVIEW_DATA,
    envelope,
    iv,
    make_settings,
)

from genomics_mcp.errors import GenomicsError, UnauthorizedError
from genomics_mcp.service import GenomicsService
from genomics_mcp.storage.s3 import S3Clients, target_for
from genomics_mcp.storage.uris import S3Location

DUMMY = {
    "AWS_ACCESS_KEY_ID": "AKIAAMBIENTDUMMY0000",
    "AWS_SECRET_ACCESS_KEY": "ambient-dummy-secret",
    "AWS_SESSION_TOKEN": "ambient-dummy-token",
    "AWS_PROFILE": "ambient-profile",
    "AWS_DEFAULT_REGION": "eu-west-3",
    "AWS_ENDPOINT_URL": "http://169.254.169.254",
    "AWS_ENDPOINT_URL_S3": "http://169.254.169.254",
    "AWS_REQUEST_PAYER": "requester",
    "HTTPS_PROXY": "http://127.0.0.1:9",
}


@pytest.fixture
def hostile_env(monkeypatch, tmp_path):
    """Ambient AWS variables and config files that must never be read."""
    cfg = tmp_path / "ambient-config"
    cfg.write_text(
        "[profile ambient-profile]\nregion = ap-south-1\nendpoint_url = http://169.254.169.254\n"
        "[default]\nregion = ap-south-1\ns3 =\n    addressing_style = virtual\n"
    )
    creds = tmp_path / "ambient-credentials"
    creds.write_text(
        "[ambient-profile]\naws_access_key_id = AKIAFILEDUMMY0000000\n"
        "aws_secret_access_key = file-secret\n"
    )
    for k, v in DUMMY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))

    seen: list[str] = []
    real_load = botocore.configloader.raw_config_parse

    def guarded(path, *a, **k):
        seen.append(str(path))
        if str(path) in (str(cfg), str(creds)):
            raise AssertionError("ambient AWS config file was read")
        return real_load(path, *a, **k)

    monkeypatch.setattr(botocore.configloader, "raw_config_parse", guarded)

    def no_chain(*a, **k):
        raise AssertionError("default credential chain was used")

    monkeypatch.setattr(botocore.credentials, "create_credential_resolver", no_chain)
    return seen


def profile_settings(tmp_path, **profile):
    base = {
        "endpoint_url": "http://127.0.0.1:39000",
        "access_key_id_env": "GENOMICS_MCP_TEST_S3_KEY",
        "secret_access_key_env": "GENOMICS_MCP_TEST_S3_SECRET",
    }
    return make_settings(
        tmp_path,
        [],
        env={
            "GENOMICS_MCP_TEST_S3_KEY": "explicit-key-id",
            "GENOMICS_MCP_TEST_S3_SECRET": "explicit-secret",
        },
        storage={"profiles": {"mine": {**base, **profile}}},
    )


def test_anonymous_client_ignores_ambient_everything(tmp_path, hostile_env):
    settings = make_settings(tmp_path, [])
    clients = S3Clients(settings, tmp_path / "iso")
    target = target_for(settings, None)
    client = clients.client(target)
    assert client.meta.endpoint_url == "https://s3.amazonaws.com"
    assert client.meta.region_name == "us-east-1"
    assert client._request_signer._credentials is None
    url = clients.presign(target, S3Location("public-bucket", "a/b.bam"))
    assert url == "https://public-bucket.s3.amazonaws.com/a/b.bam"
    assert "request-payer" not in url.lower()
    assert all("ambient" not in p for p in hostile_env)


def test_profile_uses_only_explicit_credentials(tmp_path, hostile_env):
    settings = profile_settings(tmp_path)
    clients = S3Clients(settings, tmp_path / "iso")
    target = target_for(settings, "mine")
    client = clients.client(target)
    creds = client._request_signer._credentials
    assert creds.access_key == "explicit-key-id" and creds.token is None
    assert client.meta.endpoint_url == "http://127.0.0.1:39000"
    url = clients.presign(target, S3Location("bucket", "k.bam"))
    assert "explicit-key-id" in url and "AKIA" not in url
    assert "x-amz-request-payer" not in url.lower()
    assert "X-Amz-Expires=900" in url


def test_requester_pays_only_when_profile_enables_it(tmp_path, hostile_env):
    settings = profile_settings(tmp_path, requester_pays=True)
    clients = S3Clients(settings, tmp_path / "iso")
    url = clients.presign(target_for(settings, "mine"), S3Location("bucket", "k.bam"))
    assert "x-amz-request-payer=requester" in url.lower()
    assert "x-amz-request-payer" not in url.lower().split("x-amz-signedheaders=")[1].split("&")[0]


def test_bucket_allowlist_and_disabled_public(tmp_path):
    settings = profile_settings(tmp_path, buckets=["allowed"])
    from genomics_mcp.storage.s3 import check_bucket

    with pytest.raises(UnauthorizedError):
        check_bucket(target_for(settings, "mine"), "other")
    off = make_settings(tmp_path, [], storage={"allow_public_s3": False})
    with pytest.raises(UnauthorizedError):
        target_for(off, None)


async def test_missing_credential_variables_fail_closed(tmp_path, hostile_env):
    settings = make_settings(
        tmp_path,
        [],
        storage={
            "profiles": {
                "mine": {
                    "endpoint_url": "http://127.0.0.1:39000",
                    "access_key_id_env": "GENOMICS_MCP_UNSET_KEY",
                    "secret_access_key_env": "GENOMICS_MCP_UNSET_SECRET",
                }
            }
        },
    )
    svc = GenomicsService(settings)
    try:
        res = envelope(
            await svc.call(
                "get_reads",
                {
                    "file": {"uri": "s3://bucket/k.bam", "storage_profile": "mine"},
                    "interval": iv("c", 0, 1),
                },
            )
        )
    finally:
        await svc.aclose()
    assert res["error"]["code"] == "unauthorized"
    assert "not set" in res["error"]["message"]


def _minio_up() -> bool:
    try:
        with urllib.request.urlopen(MINIO_ENDPOINT + "/minio/health/live", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


minio = pytest.mark.skipif(
    not (MINIO_KEY and MINIO_SECRET and _minio_up()),
    reason="set GENOMICS_MCP_TEST_S3_KEY/SECRET with a local MinIO to run",
)


def minio_settings(tmp_path, roots):
    return make_settings(
        tmp_path,
        roots,
        env={"GENOMICS_MCP_TEST_S3_KEY": MINIO_KEY, "GENOMICS_MCP_TEST_S3_SECRET": MINIO_SECRET},
        storage={
            "profiles": {
                "minio": {
                    "endpoint_url": MINIO_ENDPOINT,
                    "access_key_id_env": "GENOMICS_MCP_TEST_S3_KEY",
                    "secret_access_key_env": "GENOMICS_MCP_TEST_S3_SECRET",
                    "buckets": [MINIO_BUCKET],
                }
            }
        },
    )


@minio
async def test_minio_listing_and_reads_match_local(tmp_path, hostile_env):
    if not REVIEW_DATA:
        pytest.skip("set GENOMICS_MCP_TEST_FIXTURES to the local copy of the bucket files")
    from pathlib import Path

    local = Path(REVIEW_DATA)
    svc = GenomicsService(minio_settings(tmp_path, [local]))
    try:
        listing = envelope(
            await svc.call(
                "list_files",
                {"source": "s3", "accession": f"s3://{MINIO_BUCKET}/", "storage_profile": "minio"},
            )
        )
        assert listing["status"] == "ok"
        names = {r["file"]["uri"].rsplit("/", 1)[1]: r for r in listing["data"]["records"]}
        assert names["reads.bam"]["index"]["state"] == "present"
        assert names["signal.bw"]["index"]["state"] == "not_needed"
        interval = {"contig": "chrTest", "start": 0, "end": 200, "assembly": "synthetic-v1"}
        cases = [
            ("get_reads", "reads.bam", {}),
            ("get_coverage", "reads.bam", {}),
            ("get_pileup", "reads.bam", {}),
            ("get_variants", "variants.vcf.gz", {}),
            ("get_variants", "variants.bcf", {}),
            ("get_features", "features.bed.gz", {}),
            ("get_sequence", "reference.fa", {}),
            ("get_signal", "signal.bw", {}),
            ("get_reads", "reads.cram", {"reference": {"uri": str(local / "reference.fa")}}),
            ("get_reads", "reads-embedded.cram", {}),
        ]
        for op, name, extra in cases:
            a = envelope(
                await svc.call(
                    op, {"file": {"uri": str(local / name)}, "interval": interval, **extra}
                )
            )
            b = envelope(
                await svc.call(
                    op,
                    {
                        "file": {"uri": f"s3://{MINIO_BUCKET}/{name}", "storage_profile": "minio"},
                        "interval": interval,
                        **extra,
                    },
                )
            )
            assert a["status"] == "ok", (op, name, a.get("error"))
            assert b["status"] == "ok", (op, name, b.get("error"))
            assert a["data"]["records"] == b["data"]["records"], (op, name)
            text = str(b)
            assert "X-Amz-Signature" not in text and MINIO_SECRET not in text
    finally:
        await svc.aclose()


@minio
async def test_minio_wrong_secret_is_unauthorized(tmp_path, hostile_env):
    settings = make_settings(
        tmp_path,
        [],
        env={"K": "genomics-local-test", "S": "wrong-secret-value"},
        storage={
            "profiles": {
                "bad": {
                    "endpoint_url": MINIO_ENDPOINT,
                    "access_key_id_env": "K",
                    "secret_access_key_env": "S",
                }
            }
        },
    )
    svc = GenomicsService(settings)
    try:
        res = envelope(
            await svc.call(
                "get_reads",
                {
                    "file": {"uri": f"s3://{MINIO_BUCKET}/reads.bam", "storage_profile": "bad"},
                    "interval": {
                        "contig": "chrTest",
                        "start": 0,
                        "end": 10,
                        "assembly": "synthetic-v1",
                    },
                },
            )
        )
    finally:
        await svc.aclose()
    assert res["error"]["code"] == "unauthorized", res["error"]


def test_errors_are_typed():
    assert issubclass(UnauthorizedError, GenomicsError)
