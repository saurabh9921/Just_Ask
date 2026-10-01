"""Outbound host policy for user-configured database connections (SSRF defence).

A ``data_admin`` can make the API open TCP connections to a host of their choosing
(``POST /connections/{id}/test``). Without a policy that becomes a way to probe the
platform's own infrastructure (Redis, Qdrant, cloud metadata endpoints, loopback).

The policy rejects denied hostnames and any host whose *resolved* addresses fall
into denied networks. Private RFC 1918 ranges are allowed on purpose: customer ERP
databases usually live there.

Limitation: the driver resolves the name again when connecting (DNS rebinding
window). Production deployments must additionally restrict API egress with network
policies (see docs/security.md).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Final

from app.connectors.common.errors import ConnectionConfigError

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_HOSTNAME_RE: Final = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)


class HostPolicy:
    def __init__(self, denylist: list[str]) -> None:
        self._names: set[str] = set()
        self._networks: list[IPNetwork] = []
        for entry in denylist:
            item = entry.strip().lower()
            if not item:
                continue
            try:
                self._networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                self._names.add(item.rstrip("."))

    def _denied_address(self, address: IPAddress) -> bool:
        mapped = getattr(address, "ipv4_mapped", None)
        candidates: list[IPAddress] = [address] + ([mapped] if mapped is not None else [])
        return any(
            candidate.version == net.version and candidate in net
            for candidate in candidates
            for net in self._networks
        )

    def check_syntax(self, host: str) -> str:
        normalised = host.strip().lower().rstrip(".")
        if not normalised:
            raise ConnectionConfigError("A host is required.", details={"reason": "HOST_REQUIRED"})
        try:
            address = ipaddress.ip_address(normalised.strip("[]"))
        except ValueError:
            if not _HOSTNAME_RE.fullmatch(normalised):
                raise ConnectionConfigError(
                    "The host name is invalid.", details={"reason": "HOST_INVALID"}
                ) from None
        else:
            if self._denied_address(address):
                raise self._denied()
            return str(address)
        if normalised in self._names:
            raise self._denied()
        return normalised

    async def check(self, host: str, port: int | None) -> None:
        """Validate syntax and every resolved address. Unresolvable hosts are allowed
        here (the connection test reports HOST_UNREACHABLE)."""
        normalised = self.check_syntax(host)
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(normalised, port or 0, type=socket.SOCK_STREAM), timeout=5
            )
        except (OSError, TimeoutError):
            return
        for info in infos:
            raw = info[4][0]
            try:
                address = ipaddress.ip_address(str(raw).split("%", 1)[0])
            except ValueError:
                continue
            if self._denied_address(address):
                raise self._denied()

    @staticmethod
    def _denied() -> ConnectionConfigError:
        return ConnectionConfigError(
            "Connections to this host are not allowed.", details={"reason": "HOST_NOT_ALLOWED"}
        )
