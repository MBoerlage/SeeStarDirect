"""
Standard ASCOM Alpaca UDP discovery (ASCOM Alpaca spec, section "Discovery").

The client broadcasts the ASCII payload b"alpacadiscovery1" to UDP port
32227. Any Alpaca server listening on the LAN responds with a JSON datagram
containing at least {"AlpacaPort": <int>}. This is manufacturer-agnostic --
verified live against the S30 Pro on 2026-08-17 (responded with
AlpacaPort=32323).
"""

import json
import logging
import socket
import time

logger = logging.getLogger("alpaca.discovery")

DISCOVERY_MESSAGE = b"alpacadiscovery1"
DISCOVERY_PORT = 32227


def discover_servers(timeout=3, broadcast_addresses=None):
    """Broadcast the Alpaca discovery datagram and collect responses.

    Returns a list of dicts: {"ip": str, "port": int}, de-duplicated.
    """
    if broadcast_addresses is None:
        broadcast_addresses = ["255.255.255.255"]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))

    for addr in broadcast_addresses:
        try:
            sock.sendto(DISCOVERY_MESSAGE, (addr, DISCOVERY_PORT))
            logger.debug("sent discovery datagram to %s:%s", addr, DISCOVERY_PORT)
        except OSError as exc:
            logger.debug("failed to send discovery to %s: %s", addr, exc)

    found = {}
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            data, (ip, _port) = sock.recvfrom(4096)
        except socket.timeout:
            break
        except OSError:
            break
        try:
            payload = json.loads(data.decode("utf-8"))
            alpaca_port = int(payload["AlpacaPort"])
        except (ValueError, KeyError, UnicodeDecodeError, TypeError):
            logger.debug("ignoring unparseable discovery response from %s: %r", ip, data)
            continue
        logger.debug("discovery response from %s: AlpacaPort=%s", ip, alpaca_port)
        found[(ip, alpaca_port)] = {"ip": ip, "port": alpaca_port}

    sock.close()
    return list(found.values())
