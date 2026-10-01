"""Local network helpers."""

import socket


def detect_local_ip() -> str:
    """Return the primary LAN IPv4 address of this host.

    Works by opening a UDP socket toward a public address; no packets are sent,
    but the kernel picks the outbound interface, which yields the address that
    LAN clients would use to reach this server.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"
