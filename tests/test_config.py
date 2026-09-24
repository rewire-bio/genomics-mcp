from pathlib import Path

import pytest

from genomics_mcp.config import ConfigError, load_settings, validate_http_security
from genomics_mcp.errors import ErrorCode, redact

TOKEN = "t" * 40


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_default_limits_match_plan():
    s = load_settings(env={})
    assert s.limits.max_region_bp == 1_000_000
    assert s.limits.max_records == 10_000
    assert s.limits.max_response_bytes == 1024 * 1024
    assert s.limits.interactive_timeout_s == 30
    assert s.limits.max_transfer_bytes == 100 * 1024 * 1024
    assert s.http.host == "127.0.0.1"
    assert s.paths.allowed_roots == []


def test_toml_then_env_then_overrides(tmp_path):
    cfg = write(
        tmp_path,
        """
[http]
port = 9000
[limits]
max_records = 500
[paths]
allowed_roots = ["/tmp/a"]
""",
    )
    s = load_settings(
        cfg,
        env={"GENOMICS_MCP_PORT": "9100", "GENOMICS_MCP_ALLOWED_ROOTS": "/tmp/b:/tmp/c"},
        overrides={"http": {"port": 9200}},
    )
    assert s.http.port == 9200
    assert s.limits.max_records == 500
    assert [p.name for p in s.paths.allowed_roots] == ["b", "c"]


def test_unknown_keys_and_inline_tokens_are_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, '[http]\ntoken = "abc"\n'), env={})
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, "[limitz]\nx = 1\n"), env={})


def test_aws_env_names_are_refused(tmp_path):
    cfg = write(
        tmp_path,
        """
[storage.profiles.minio]
endpoint_url = "http://127.0.0.1:9000"
access_key_id_env = "AWS_ACCESS_KEY_ID"
secret_access_key_env = "AWS_SECRET_ACCESS_KEY"
""",
    )
    with pytest.raises(ConfigError, match="AWS_"):
        load_settings(cfg, env={})


def test_s3_credentials_only_from_named_variables(tmp_path):
    cfg = write(
        tmp_path,
        """
[storage.profiles.minio]
endpoint_url = "http://127.0.0.1:9000"
access_key_id_env = "MINIO_TEST_KEY"
secret_access_key_env = "MINIO_TEST_SECRET"

[storage.profiles.open]
endpoint_url = "https://s3.amazonaws.com"
anonymous = true
""",
    )
    ambient = {
        "AWS_ACCESS_KEY_ID": "AKIAAMBIENTAMBIENT00",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret-value",
        "AWS_PROFILE": "work",
    }
    s = load_settings(cfg, env=ambient)
    assert s.s3_credentials("open") is None
    with pytest.raises(ConfigError) as exc:
        s.s3_credentials("minio")
    assert exc.value.info.code is ErrorCode.UNAUTHORIZED

    s = load_settings(
        cfg, env={**ambient, "MINIO_TEST_KEY": "minioadmin", "MINIO_TEST_SECRET": "minio-pass-1234"}
    )
    creds = s.s3_credentials("minio")
    assert creds is not None
    assert creds.access_key_id.get_secret_value() == "minioadmin"
    assert "minio-pass-1234" not in repr(creds)
    assert "minio-pass-1234" not in redact("oops minio-pass-1234")
    assert "ambient" not in str(s.public_view())


def test_non_anonymous_profile_needs_named_credentials(tmp_path):
    cfg = write(tmp_path, '[storage.profiles.p]\nendpoint_url = "https://s3.example.org"\n')
    with pytest.raises(ConfigError, match="access_key_id_env"):
        load_settings(cfg, env={})


def test_http_requires_token():
    with pytest.raises(ConfigError, match="bearer token"):
        validate_http_security(load_settings(env={}))
    with pytest.raises(ConfigError, match="32"):
        validate_http_security(load_settings(env={"GENOMICS_MCP_HTTP_TOKEN": "short"}))
    token = validate_http_security(load_settings(env={"GENOMICS_MCP_HTTP_TOKEN": TOKEN}))
    assert token.get_secret_value() == TOKEN


def test_http_token_file(tmp_path):
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    s = load_settings(env={}, overrides={"http": {"token_file": str(f)}})
    assert validate_http_security(s).get_secret_value() == TOKEN


def test_non_loopback_needs_explicit_opt_in():
    env = {"GENOMICS_MCP_HTTP_TOKEN": TOKEN, "GENOMICS_MCP_HOST": "0.0.0.0"}
    with pytest.raises(ConfigError, match="non-loopback"):
        validate_http_security(load_settings(env=env))
    s = load_settings(env=env, overrides={"http": {"allow_non_loopback": True}})
    validate_http_security(s)


def test_public_view_has_no_secrets():
    s = load_settings(env={"GENOMICS_MCP_HTTP_TOKEN": TOKEN})
    view = s.public_view()
    assert view["http"]["token_configured"] is True
    assert TOKEN not in str(view)


def test_example_config_loads():
    root = Path(__file__).resolve().parents[1]
    s = load_settings(root / "config.example.toml", env={})
    assert s.limits.max_region_bp == 1_000_000
    assert s.http.host == "127.0.0.1"
    assert "mcp-name: io.github.rewire-bio/genomics-mcp" in (root / "README.md").read_text()
