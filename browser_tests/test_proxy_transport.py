"""Small valid HTTP/2 exchange through the actual TLS proxy; no attack traffic."""
import json
import socket

import httpx

from test_login_caddy import ORIGIN, stack


def test_https_redirect_and_verified_http2_routing(stack):
    with httpx.Client(trust_env=False, follow_redirects=False) as direct:
        redirect = direct.get("http://localhost:8080/health/ready")
    assert redirect.status_code == 301
    assert redirect.headers["location"] == ORIGIN + "/health/ready"

    # Use the generated CA with hostname verification, and negotiate HTTP/2 by
    # ALPN. One ordinary GET needs only static/literal HPACK fields; no extra
    # HTTP/2 client dependency or malformed/flooding traffic is necessary.
    context = stack.test_ssl_context
    context.set_alpn_protocols(["h2"])

    def frame(kind, flags, stream, payload=b""):
        return len(payload).to_bytes(3, "big") + bytes([kind, flags]) + stream.to_bytes(4, "big") + payload

    with socket.create_connection(("localhost", 8443), timeout=5) as tcp:
        with context.wrap_socket(tcp, server_hostname="localhost") as tls:
            assert tls.selected_alpn_protocol() == "h2"
            authority, path = b"localhost:8443", b"/health/ready"
            headers = b"\x82\x87\x01" + bytes([len(authority)]) + authority
            headers += b"\x04" + bytes([len(path)]) + path
            tls.sendall(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n" + frame(4, 0, 0) + frame(1, 5, 1, headers))

            def read_exact(size):
                result = b""
                while len(result) < size:
                    chunk = tls.recv(size - len(result))
                    assert chunk, "HTTP/2 connection closed before response completed"
                    result += chunk
                return result

            body, got_headers = b"", False
            for _ in range(32):
                header = read_exact(9)
                size, kind, flags = int.from_bytes(header[:3], "big"), header[3], header[4]
                stream = int.from_bytes(header[5:], "big") & 0x7fffffff
                assert size <= 65536
                payload = read_exact(size)
                assert kind not in (3, 7), "Proxy reset the stream or sent GOAWAY"
                if kind == 4 and not flags & 1:
                    tls.sendall(frame(4, 1, 0))
                if stream == 1:
                    if kind == 1:
                        got_headers = True
                    elif kind == 0:
                        assert not flags & 8, "Unexpected padded DATA frame"
                        body += payload
                    if kind in (0, 1) and flags & 1:
                        break
            else:
                raise AssertionError("HTTP/2 response did not finish")
            assert got_headers
            assert json.loads(body) == {"status": "ready"}
