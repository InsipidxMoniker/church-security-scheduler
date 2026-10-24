"""One client-IP trust boundary: socket peer -> allowlisted Caddy -> X-Real-IP.

Uvicorn must run with --no-proxy-headers so request.client remains the TCP peer.
Cloudflare/X-Forwarded-For/Forwarded are never consumed by the application.
"""
import ipaddress

from scheduler.security import Problem


def address(value):
    # Scoped IPv6 addresses must not create arbitrarily many throttle identities.
    if "%" in value:
        raise ValueError("Scoped address")
    result = ipaddress.ip_address(value)
    return result.ipv4_mapped if isinstance(result, ipaddress.IPv6Address) and result.ipv4_mapped else result


def client_ip(request, trusted):
    peer = request.client.host if request.client else "unknown"
    try:
        socket_address = address(peer)
    except ValueError:
        # Non-IP names occur in the test transport; no forwarding trust applies.
        return peer
    if not any(socket_address in network for network in trusted):
        return str(socket_address)
    values = request.headers.getlist("x-real-ip")
    try:
        if len(values) != 1:
            raise ValueError("Missing/duplicate normalized header")
        return str(address(values[0]))
    except ValueError:
        # Never select a different forwarded header or split an ambiguous chain.
        raise Problem(400, "Invalid proxy client address") from None
