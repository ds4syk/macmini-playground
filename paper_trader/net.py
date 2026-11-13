"""Network helpers.

macOS may hand out IPv6 addresses first even when the IPv6 route is blackholed;
this is a common cause of urllib hangs against multi-homed Chinese API hosts.
Force IPv4 for the provider calls used by this single-purpose process.
"""
from __future__ import annotations

import socket

_ORIGINAL = socket.getaddrinfo


def install_ipv4_preference() -> None:
    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _ORIGINAL(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo


install_ipv4_preference()
