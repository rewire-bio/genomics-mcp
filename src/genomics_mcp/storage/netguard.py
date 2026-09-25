"""Destination checks for storage requests (every hop, including redirects).

Rules, on top of `security.check_network_destination`:

- Cloud metadata endpoints (by name or any link-local address) are always refused, also when a
  hostname merely resolves to one.
- Hosts resolving to loopback/private/non-global addresses, and plain http, are refused unless
  the host is listed in `storage.local_network_hosts` or is an explicitly configured S3
  profile endpoint.
- The connected peer address is checked again after connecting.

Error messages name the host only, never the URL.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlsplit

from genomics_mcp.config import Settings
from genomics_mcp.errors import NotFoundError, UnauthorizedError, UpstreamError
from genomics_mcp.security import check_network_destination, is_metadata_destination


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").strip("[]").rstrip(".").lower()


def trusted_hosts(settings: Settings, extra: Iterable[str] = ()) -> frozenset[str]:
    hosts = {h.strip("[]").rstrip(".").lower() for h in settings.storage.local_network_hosts}
    for prof in settings.storage.profiles.values():
        hosts.add(_host(prof.endpoint_url))
    hosts.update(h.lower() for h in extra)
    return frozenset(h for h in hosts if h)


def _non_global(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global


def check_address(ip_text: str, *, host: str, trusted: bool, source: str) -> None:
    """Refuse metadata addresses always; non-global addresses unless the host is trusted."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return
    if is_metadata_destination(str(ip)):
        raise UnauthorizedError(
            f"{source}: {host} resolves to a cloud metadata or link-local address; refused",
            source=source,
            details={"host": host},
        )
    if not trusted and _non_global(ip):
        raise UnauthorizedError(
            f"{source}: {host} resolves to a loopback/private address",
            source=source,
            hint="list the host in storage.local_network_hosts to allow a local server",
            details={"host": host},
        )


async def check_destination(
    url: str,
    settings: Settings,
    *,
    source: str,
    previous_url: str | None = None,
    extra_trusted: Iterable[str] = (),
) -> None:
    """Validate one hop before it is sent (scheme, host, DNS answers)."""
    host = _host(url)
    trusted = host in trusted_hosts(settings, extra_trusted)
    check_network_destination(
        url, source=source, allowed_hosts=None, allow_http=trusted, previous_url=previous_url
    )
    port = urlsplit(url).port or (443 if url.lower().startswith("https") else 80)
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise NotFoundError(
                f"{source}: host {host} could not be resolved",
                source=source,
                details={"host": host},
            ) from None
        except OSError as exc:
            raise UpstreamError(
                f"{source}: DNS lookup for {host} failed ({type(exc).__name__})", source=source
            ) from None
        addresses = [str(info[4][0]) for info in infos]
    for addr in addresses:
        check_address(addr, host=host, trusted=trusted, source=source)


def check_peer(
    peer: tuple | None, url: str, settings: Settings, *, source: str, extra_trusted=()
) -> None:
    """Re-check the connected peer (defends against DNS answers changing after the check)."""
    if not peer:
        return
    host = _host(url)
    check_address(
        str(peer[0]),
        host=host,
        trusted=host in trusted_hosts(settings, extra_trusted),
        source=source,
    )
