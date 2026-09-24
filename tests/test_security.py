import pytest

from genomics_mcp.errors import InvalidInputError, NotFoundError, UnauthorizedError
from genomics_mcp.security import resolve_local_path, scrubbed_env


def test_local_paths_must_be_under_allowed_roots(settings, data_root, tmp_path):
    f = data_root / "a.bam"
    f.write_bytes(b"x")
    assert resolve_local_path(settings, str(f)) == f.resolve()
    assert resolve_local_path(settings, f"file://{f}") == f.resolve()
    outside = tmp_path / "outside.bam"
    outside.write_bytes(b"x")
    with pytest.raises(UnauthorizedError):
        resolve_local_path(settings, str(outside))
    with pytest.raises(InvalidInputError):
        resolve_local_path(settings, "a.bam")
    with pytest.raises(NotFoundError):
        resolve_local_path(settings, str(data_root / "missing.bam"))


def test_symlink_cannot_escape_allowed_root(settings, data_root, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    link = data_root / "link.bam"
    link.symlink_to(secret)
    with pytest.raises(UnauthorizedError):
        resolve_local_path(settings, str(link))


def test_dotdot_cannot_escape(settings, data_root, tmp_path):
    (tmp_path / "x.bam").write_bytes(b"x")
    with pytest.raises(UnauthorizedError):
        resolve_local_path(settings, str(data_root / ".." / "x.bam"))


def test_scrubbed_env_removes_ambient_credentials(tmp_path):
    env = scrubbed_env(
        {
            "AWS_ACCESS_KEY_ID": "AKIA...",
            "AWS_PROFILE": "work",
            "GOOGLE_APPLICATION_CREDENTIALS": "/x",
            "PATH": "/bin",
        },
        empty_aws_config_dir=tmp_path / "aws",
    )
    assert "AWS_ACCESS_KEY_ID" not in env and "AWS_PROFILE" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert env["PATH"] == "/bin"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert (tmp_path / "aws" / "credentials").read_text() == ""


def test_scrubbed_env_isolates_cram_reference_and_boto_config(tmp_path):
    env = scrubbed_env(
        {
            "REF_PATH": "/shared/refs/%2s/%s",
            "REF_CACHE": "/shared/cache",
            "BOTO_CONFIG": "/etc/boto.cfg",
            "PATH": "/bin",
        },
        empty_aws_config_dir=tmp_path / "aws",
        empty_ref_dir=tmp_path / "refs",
    )
    assert env["REF_PATH"] == env["REF_CACHE"] == str(tmp_path / "refs")
    assert list((tmp_path / "refs").iterdir()) == []
    assert env["BOTO_CONFIG"] == str(tmp_path / "aws" / "boto")
    assert (tmp_path / "aws" / "boto").read_text() == ""
    bare = scrubbed_env({"REF_PATH": "/x", "REF_CACHE": "/y", "BOTO_CONFIG": "/z"})
    assert not {"REF_PATH", "REF_CACHE", "BOTO_CONFIG"} & set(bare)


def test_metadata_destination_detection():
    from genomics_mcp.security import is_metadata_destination

    for host in ("169.254.169.254", "169.254.170.2", "[fd00:ec2::254]", "INSTANCE-DATA", "fe80::1"):
        assert is_metadata_destination(host), host
    for host in ("127.0.0.1", "www.ebi.ac.uk", "10.0.0.1"):
        assert not is_metadata_destination(host), host
