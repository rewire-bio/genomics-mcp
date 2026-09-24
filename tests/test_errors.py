from genomics_mcp.errors import (
    ErrorCode,
    NotFoundError,
    redact,
    redact_obj,
    register_secret,
)


def test_redacts_presigned_s3_urls():
    url = (
        "https://b.s3.amazonaws.com/k.bam?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260924&X-Amz-Signature=deadbeef"
    )
    out = redact(f"failed to open {url} after retry")
    assert "deadbeef" not in out and "AKIA" not in out
    assert out.startswith("failed to open https://b.s3.amazonaws.com/k.bam?")


def test_redacts_userinfo_tokens_and_key_ids():
    out = redact("GET https://alice:hunter2@ega.example/f?token=abc123&page=2")
    assert "hunter2" not in out and "abc123" not in out and "page=2" in out
    assert "xyzxyzxyzxyz" not in redact("Authorization: Bearer xyzxyzxyzxyz")
    assert "AKIAIOSFODNN7EXAMPLE" not in redact("key AKIAIOSFODNN7EXAMPLE leaked")
    assert "s3cr3tvalue" not in redact("aws_secret_access_key = s3cr3tvalue")


def test_registered_secret_is_redacted_anywhere():
    register_secret("personal-minio-secret-123")
    assert "personal-minio-secret-123" not in redact("boom personal-minio-secret-123 boom")


def test_redact_obj_drops_secret_keys_but_keeps_ordinary_ones():
    out = redact_obj({"api_key": "k", "key": "bam", "nested": [{"password": "p", "n": 1}]})
    assert out["api_key"] == "[REDACTED]"
    assert out["key"] == "bam"
    assert out["nested"][0] == {"password": "[REDACTED]", "n": 1}


def test_error_info_is_structured_and_redacted():
    err = NotFoundError("no file at https://u:p@h/x?sig=1", source="ena")
    assert err.info.code is ErrorCode.NOT_FOUND
    assert err.info.source == "ena"
    assert "u:p" not in err.info.message
    assert err.info.retryable is False
