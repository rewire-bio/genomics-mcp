"""Command line entry point: `genomics-mcp`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

import anyio

from genomics_mcp import __version__
from genomics_mcp.auth import BearerAuthMiddleware
from genomics_mcp.config import ConfigError, Settings, load_settings, validate_http_security
from genomics_mcp.logs import configure_logging


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="genomics-mcp",
        description="MCP server for bounded genomic data retrieval and reference evidence.",
        epilog=(
            "HTTP transport requires a bearer token of at least 32 characters in "
            "GENOMICS_MCP_HTTP_TOKEN (or http.token_file) and binds 127.0.0.1 by default."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", metavar="PATH", help="TOML config file (or GENOMICS_MCP_CONFIG).")
    p.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (default) or authenticated Streamable HTTP.",
    )
    p.add_argument("--host", help="HTTP bind host (default 127.0.0.1).")
    p.add_argument("--port", type=int, help="HTTP port (default 8765).")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration, print a non-secret summary and capabilities, then exit.",
    )
    return p


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.host:
        out.setdefault("http", {})["host"] = args.host
    if args.port:
        out.setdefault("http", {})["port"] = args.port
    if args.log_level:
        out["logging"] = {"level": args.log_level}
    return out


async def _serve_stdio(settings: Settings) -> None:
    from genomics_mcp.server import build_server
    from genomics_mcp.service import GenomicsService

    service = GenomicsService(settings)
    try:
        await build_server(service).run_stdio_async()
    finally:
        await service.aclose()


async def _serve_http(settings: Settings) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from genomics_mcp.server import build_server
    from genomics_mcp.service import GenomicsService

    token = validate_http_security(settings)
    service = GenomicsService(settings)
    host, port = settings.http.host, settings.http.port
    hosts = {f"{host}:*", "127.0.0.1:*", "localhost:*", "[::1]:*", *settings.http.allowed_hosts}
    if not settings.http.is_loopback:
        hosts = {f"{host}:*", *settings.http.allowed_hosts}
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(f"http://{h}" for h in hosts),
    )
    app = build_server(service).streamable_http_app(
        streamable_http_path=settings.http.path, transport_security=security, host=host
    )
    config = uvicorn.Config(
        BearerAuthMiddleware(app, token),
        host=host,
        port=port,
        log_level=settings.logging.level.lower(),
        access_log=False,
        log_config=None,
        server_header=False,
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        await service.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config, overrides=_overrides(args))
        configure_logging(settings.logging.level)
        if args.check_config:
            from genomics_mcp.service import GenomicsService

            service = GenomicsService(settings)
            if args.transport == "http":
                validate_http_security(settings)
            print(
                json.dumps({**service.status(), "capabilities": service.capabilities()}, indent=2)
            )
            return 0
        if args.transport == "http":
            validate_http_security(settings)
            anyio.run(_serve_http, settings)
        else:
            anyio.run(_serve_stdio, settings)
    except ConfigError as exc:
        hint = f" ({exc.info.hint})" if exc.info.hint else ""
        print(f"genomics-mcp: {exc.info.message}{hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
