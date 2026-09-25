"""Local FASTA reference boundaries: every file the native reader may open is allowlisted
(symlinks resolved), indexes are never created implicitly, and non-local references are
refused before any resolver or network use."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pysam
import pytest

from genomics_mcp.errors import PreparationRequiredError
from genomics_mcp.evidence import ReferenceRuntime, register
from genomics_mcp.models import FileRef, VariantSpec, Visibility
from genomics_mcp.public import EgressContext
from genomics_mcp.registry import Operation, Registry
from genomics_mcp.result import ResultStatus
from genomics_mcp.service import GenomicsService

from .conftest import Router
from .test_mcp_integration import _ctx

SEQ = "GGCTCACACAGTT"
SNV = {"assembly": "GRCh38", "contig": "7", "pos": 4, "ref": "T", "alt": "G"}


class RecordingResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, file, ctx):
        self.calls.append(f"resolve {file.uri}")
        raise AssertionError("resolver must not be reached for a reference FASTA")

    async def stat(self, file, ctx):
        self.calls.append(f"stat {file.uri}")
        raise AssertionError("resolver must not be reached for a reference FASTA")


def service(settings, router: Router, resolver: RecordingResolver | None = None) -> GenomicsService:
    reg = Registry()
    if resolver is not None:
        reg.register_resolver("https", resolver, provider="test")
    client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle))
    register(reg, runtime=ReferenceRuntime(client=client))
    return GenomicsService(settings, reg, load_providers=False)


@pytest.fixture
def native_opens(monkeypatch) -> list[tuple]:
    """Record every pysam.FastaFile open (and still open it)."""
    calls: list[tuple] = []
    real = pysam.FastaFile

    def recording(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(pysam, "FastaFile", recording)
    return calls


def write_fasta(directory: Path, name: str = "ref.fa") -> Path:
    path = directory / name
    path.write_text(f">chr7\n{SEQ}\n")
    return path


def write_bgzf(directory: Path, name: str = "ref.fa.gz", *, index: bool = True) -> Path:
    plain = write_fasta(directory, "plain_for_bgzf.fa")
    path = directory / name
    pysam.tabix_compress(str(plain), str(path), force=True)
    if index:
        pysam.faidx(str(path))
    return path


async def normalize(svc: GenomicsService, reference: dict, sources=None):
    args = {"variant": SNV, "reference": reference}
    if sources is not None:
        args["sources"] = sources
    return await svc.call(Operation.NORMALIZE_VARIANT, args)


def local_error(res):
    return next(e for e in res.errors if e.source == "local_fasta")


async def test_plain_fasta_with_allowed_index_verifies_ref(
    settings, router, data_root, native_opens
):
    ref = write_fasta(data_root)
    pysam.faidx(str(ref))
    res = await normalize(service(settings, router), {"uri": str(ref), "assembly": "GRCh38"})
    assert res.status is ResultStatus.OK
    assert res.data["canonical_variant"]["reference_check"]["status"] == "verified"
    ((_args, kwargs),) = native_opens
    assert kwargs["filepath_index"] == str(ref) + ".fai"  # explicit, never implicit
    assert router.calls == []


async def test_implicit_fai_symlinked_outside_roots_is_refused_unread(
    settings, router, data_root, tmp_path, native_opens
):
    outside = tmp_path / "outside"
    outside.mkdir()
    decoy = write_fasta(outside)
    pysam.faidx(str(decoy))  # a valid index, so reading it would have "verified" REF
    ref = write_fasta(data_root)
    os.symlink(outside / "ref.fa.fai", data_root / "ref.fa.fai")
    res = await normalize(service(settings, router), {"uri": str(ref), "assembly": "GRCh38"},
                          sources=["local_fasta"])  # fmt: skip
    err = local_error(res)
    assert err.code == "unauthorized"
    assert res.data["canonical_variant"]["reference_check"]["status"] == "not_checked"
    assert native_opens == []  # the outside index was never handed to the native reader


async def test_explicit_outside_index_is_refused(
    settings, router, data_root, tmp_path, native_opens
):
    outside = tmp_path / "outside"
    outside.mkdir()
    pysam.faidx(str(write_fasta(outside)))
    ref = write_fasta(data_root)
    res = await normalize(
        service(settings, router),
        {"uri": str(ref), "index_uri": str(outside / "ref.fa.fai"), "assembly": "GRCh38"},
        sources=["local_fasta"],
    )
    assert local_error(res).code == "unauthorized"
    assert native_opens == []


async def test_missing_index_is_not_created(settings, router, data_root, native_opens):
    ref = write_fasta(data_root)
    res = await normalize(service(settings, router), {"uri": str(ref), "assembly": "GRCh38"},
                          sources=["local_fasta"])  # fmt: skip
    err = local_error(res)
    assert err.code == "unsupported" and ".fai" in err.message
    assert not (data_root / "ref.fa.fai").exists()
    assert native_opens == []


async def test_symlinked_fasta_inside_roots_works(settings, router, data_root):
    real_dir = data_root / "real"
    real_dir.mkdir()
    real = write_fasta(real_dir)
    pysam.faidx(str(real))
    link = data_root / "link.fa"
    os.symlink(real, link)
    res = await normalize(service(settings, router), {"uri": str(link), "assembly": "GRCh38"})
    assert res.data["canonical_variant"]["reference_check"]["status"] == "verified"


async def test_bgzf_reference_uses_allowlisted_fai_and_gzi(
    settings, router, data_root, native_opens
):
    ref = write_bgzf(data_root)
    res = await normalize(service(settings, router), {"uri": str(ref), "assembly": "GRCh38"})
    assert res.data["canonical_variant"]["reference_check"]["status"] == "verified"
    ((_args, kwargs),) = native_opens
    assert kwargs["filepath_index"] == str(ref) + ".fai"
    assert kwargs["filepath_index_compressed"] == str(ref) + ".gzi"


async def test_bgzf_missing_gzi_is_not_created(settings, router, data_root, native_opens):
    ref = write_bgzf(data_root, index=False)
    pysam.faidx(str(ref))
    (data_root / "ref.fa.gz.gzi").unlink()
    res = await normalize(service(settings, router), {"uri": str(ref), "assembly": "GRCh38"},
                          sources=["local_fasta"])  # fmt: skip
    err = local_error(res)
    assert err.code == "unsupported" and ".gzi" in err.message
    assert not (data_root / "ref.fa.gz.gzi").exists()
    assert native_opens == []


async def test_bgzf_gzi_symlinked_outside_roots_is_refused(
    settings, router, data_root, tmp_path, native_opens
):
    outside = tmp_path / "outside"
    outside.mkdir()
    decoy = write_bgzf(outside)
    ref = write_bgzf(data_root)
    (data_root / "ref.fa.gz.gzi").unlink()
    os.symlink(Path(str(decoy) + ".gzi"), data_root / "ref.fa.gz.gzi")
    res = await normalize(service(settings, router), {"uri": str(ref), "assembly": "GRCh38"},
                          sources=["local_fasta"])  # fmt: skip
    assert local_error(res).code == "unauthorized"
    assert native_opens == []


async def test_remote_reference_is_rejected_before_resolver_or_network(settings, router):
    resolver = RecordingResolver()
    svc = service(settings, router, resolver)
    res = await normalize(svc, {"uri": "https://example.org/GRCh38.fa", "assembly": "GRCh38"})
    assert res.status is ResultStatus.ERROR
    assert res.error.code == "preparation_required"
    assert "fetch_file" in res.error.hint
    assert resolver.calls == [] and router.calls == []
    assert resolver.calls == [] and router.calls == []


async def test_facade_rejects_remote_reference_for_private_input_without_consent(
    settings, router, data_root
):
    resolver = RecordingResolver()
    svc = service(settings, router, resolver)
    facade = svc.registry.component("reference_evidence")
    ctx = _ctx(svc, settings)
    private = FileRef(uri=str(data_root / "calls.vcf"), visibility=Visibility.PRIVATE)
    egress = EgressContext.for_files([private], consent=False)
    remote = FileRef(uri="https://example.org/GRCh38.fa", assembly="GRCh38")
    for call in (facade.normalize_variant, facade.lookup_variant):
        with pytest.raises(PreparationRequiredError):
            await call(ctx, VariantSpec(**SNV), egress=egress, reference=remote)
    assert resolver.calls == [] and router.calls == []
