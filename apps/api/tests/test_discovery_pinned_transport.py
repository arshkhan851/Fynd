import ipaddress
import socket
import ssl

import httpcore
import httpx
import pytest

from app.discovery.http import BoundedHttpClient, FetchError
from app.discovery.pinned_transport import PinnedHTTPTransport, PinnedNetworkBackend


def _record(address, family=None):
    parsed = ipaddress.ip_address(address)
    family = family or (socket.AF_INET if parsed.version == 4 else socket.AF_INET6)
    sockaddr = (address, 443) if family == socket.AF_INET else (address, 443, 0, 0)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class FakeSocket:
    def __init__(self, family, responses=(), connect_error=None, recv_error=None):
        self.family = family
        self.responses = list(responses)
        self.connect_error = connect_error
        self.recv_error = recv_error
        self.connected_to = None
        self.sent = []
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def setsockopt(self, *_args):
        pass

    def bind(self, _address):
        pass

    def connect(self, address):
        self.connected_to = address
        if self.connect_error:
            raise self.connect_error

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        if self.recv_error:
            raise self.recv_error
        if self.responses:
            return self.responses.pop(0)
        return b""

    def close(self):
        self.closed = True

    def getsockname(self):
        return ("192.0.2.10", 12345)

    def getpeername(self):
        return self.connected_to


class FakeTLSContext:
    def __init__(self):
        self.alpn = None
        self.server_names = []

    def set_alpn_protocols(self, protocols):
        self.alpn = protocols

    def wrap_socket(self, connection, *, server_hostname):
        self.server_names.append(server_hostname)
        return connection


class RejectingTLSContext(FakeTLSContext):
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    def __init__(self, expected_hostname):
        super().__init__()
        self.expected_hostname = expected_hostname
        self.wrap_attempts = 0

    def wrap_socket(self, connection, *, server_hostname):
        self.wrap_attempts += 1
        self.server_names.append(server_hostname)
        if server_hostname != self.expected_hostname:
            raise ssl.SSLCertVerificationError("certificate hostname mismatch")
        return connection


def _response(body=b"ok", *, status=200, headers=b""):
    return (
        f"HTTP/1.1 {status} {'Found' if status == 302 else 'OK'}\r\n".encode()
        + headers
        + f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n".encode()
        + body
    )


def test_connects_to_validated_public_ipv4_numeric_address():
    sockets = []

    def make_socket(family, _socktype, _proto):
        result = FakeSocket(family)
        sockets.append(result)
        return result

    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [_record("93.184.216.34")],
        socket_factory=make_socket,
    )
    stream = backend.connect_tcp("events.example.test", 443, timeout=3.0)
    assert sockets[0].connected_to == ("93.184.216.34", 443)
    assert sockets[0].connected_to[0] != "events.example.test"
    stream.close()


def test_connects_to_validated_public_ipv6_numeric_address():
    sockets = []

    def make_socket(family, _socktype, _proto):
        result = FakeSocket(family)
        sockets.append(result)
        return result

    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [_record("2606:4700:4700::1111")],
        socket_factory=make_socket,
    )
    stream = backend.connect_tcp("events.example.test", 443)
    assert sockets[0].family == socket.AF_INET6
    assert sockets[0].connected_to == ("2606:4700:4700::1111", 443, 0, 0)
    stream.close()


def test_system_resolver_requests_ipv4_and_ipv6_stream_records(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    calls = []

    def getaddrinfo(host, port, *, family, type):
        calls.append((host, port, family, type))
        return [_record("93.184.216.34")]

    monkeypatch.setattr(pinned_transport.socket, "getaddrinfo", getaddrinfo)
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family)
        sockets.append(connection)
        return connection

    backend = PinnedNetworkBackend(socket_factory=make_socket)
    stream = backend.connect_tcp("events.example.test", 443)
    assert calls == [("events.example.test", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)]
    assert sockets[0].connected_to == ("93.184.216.34", 443)
    stream.close()


def test_connection_fallback_uses_only_prevalidated_numeric_addresses():
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(
            family,
            connect_error=TimeoutError("first address timed out") if not sockets else None,
        )
        sockets.append(connection)
        return connection

    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [
            _record("93.184.216.34"),
            _record("1.1.1.1"),
        ],
        socket_factory=make_socket,
    )
    stream = backend.connect_tcp("events.example.test", 443)
    assert [connection.connected_to for connection in sockets] == [
        ("93.184.216.34", 443),
        ("1.1.1.1", 443),
    ]
    stream.close()


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.1.2",
        "224.0.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "100.64.0.1",
        "192.88.99.0",
        "192.88.99.1",
        "192.88.99.255",
        "255.255.255.255",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:8.8.8.8",
        "::8.8.8.8",
        "2002:0808:0808::1",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        "64:ff9b::808:808",
        "64:ff9b:1::808:808",
        "fe80::1%3",
    ],
)
def test_rejects_prohibited_dns_addresses_before_opening_socket(address):
    socket_calls = []
    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [_record(address)],
        socket_factory=lambda *args: socket_calls.append(args),
    )
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("events.example.test", 443)
    assert socket_calls == []


def test_rejects_mixed_public_and_prohibited_dns_answers_before_connecting():
    socket_calls = []
    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [_record("93.184.216.34"), _record("127.0.0.1")],
        socket_factory=lambda *args: socket_calls.append(args),
    )
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("events.example.test", 443)
    assert socket_calls == []


@pytest.mark.parametrize(
    "resolver",
    [
        lambda host, port: [],
        lambda host, port: (_ for _ in ()).throw(OSError("resolver unavailable")),
        lambda host, port: [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", ("/tmp/x",))],
        lambda host, port: [_record("93.184.216.34", family=socket.AF_INET6)],
    ],
)
def test_rejects_empty_failed_unsupported_or_mismatched_resolution(resolver):
    backend = PinnedNetworkBackend(resolver=resolver)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("events.example.test", 443)


def test_dns_answer_limit_fails_closed_before_opening_a_socket():
    socket_calls = []
    answers = [_record("93.184.216.34")] * 16 + [_record("127.0.0.1")]
    backend = PinnedNetworkBackend(
        resolver=lambda host, port: iter(answers),
        socket_factory=lambda *args: socket_calls.append(args),
    )
    with pytest.raises(httpcore.ConnectError, match="more than 16"):
        backend.connect_tcp("events.example.test", 443)
    assert socket_calls == []


def test_dns_answer_limit_accepts_sixteen_answers():
    sockets = []
    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [_record("93.184.216.34")] * 16,
        socket_factory=lambda family, _kind, _proto: (
            sockets.append(FakeSocket(family)) or sockets[-1]
        ),
    )
    stream = backend.connect_tcp("events.example.test", 443)
    assert len(sockets) == 1
    assert sockets[0].connected_to == ("93.184.216.34", 443)
    stream.close()


def test_httpcore_preserves_logical_host_and_tls_name_while_socket_is_pinned():
    tls = FakeTLSContext()
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family, [_response()])
        sockets.append(connection)
        return connection

    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [_record("93.184.216.34")],
        socket_factory=make_socket,
    )
    pool = httpcore.ConnectionPool(ssl_context=tls, network_backend=backend)
    request = httpcore.Request(
        "GET",
        "https://events.example.test/event",
        headers=[(b"Host", b"events.example.test")],
    )
    response = pool.handle_request(request)
    assert response.read() == b"ok"
    assert sockets[0].connected_to == ("93.184.216.34", 443)
    assert tls.server_names == ["events.example.test"]
    wire_request = b"".join(sockets[0].sent)
    assert b"Host: events.example.test" in wire_request
    response.close()
    pool.close()


def test_default_tls_context_requires_certificate_and_hostname_verification():
    context = httpcore.default_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_tls_hostname_mismatch_is_propagated_and_closes_socket(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    tls = RejectingTLSContext(expected_hostname="expected.example.test")
    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", lambda: tls)
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family)
        sockets.append(connection)
        return connection

    with BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(
            resolver=lambda host, port: [_record("93.184.216.34")],
            socket_factory=make_socket,
        ),
        max_retries=0,
        min_request_interval=0,
    ) as client:
        with pytest.raises(FetchError, match="ConnectError"):
            client.get("https://events.example.test/event")

    assert tls.verify_mode == ssl.CERT_REQUIRED
    assert tls.check_hostname is True
    assert tls.server_names == ["events.example.test"]
    assert tls.wrap_attempts == 1
    assert len(sockets) == 1
    assert sockets[0].closed is True


def test_tcp_setup_failure_closes_socket():
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family, connect_error=TimeoutError("connect timeout"))
        sockets.append(connection)
        return connection

    backend = PinnedNetworkBackend(
        resolver=lambda host, port: [_record("93.184.216.34")],
        socket_factory=make_socket,
    )
    with pytest.raises(httpcore.ConnectTimeout):
        backend.connect_tcp("events.example.test", 443)
    assert len(sockets) == 1
    assert sockets[0].closed is True


def test_response_stream_failure_closes_connection_and_is_translated(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", FakeTLSContext)
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(
            family,
            responses=[b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\npartial"],
            recv_error=OSError("simulated stream failure"),
        )
        sockets.append(connection)
        return connection

    with BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(
            resolver=lambda host, port: [_record("93.184.216.34")],
            socket_factory=make_socket,
        ),
        max_retries=0,
        min_request_interval=0,
    ) as client:
        with pytest.raises(FetchError, match="ReadError"):
            client.get("https://events.example.test/stream")

    assert len(sockets) == 1
    assert sockets[0].closed is True


def test_pool_shutdown_closes_keepalive_socket(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", FakeTLSContext)
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family, [_response()])
        sockets.append(connection)
        return connection

    client = BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(
            resolver=lambda host, port: [_record("93.184.216.34")],
            socket_factory=make_socket,
        ),
        min_request_interval=0,
    )
    assert client.get("https://events.example.test/keepalive") == b"ok"
    assert sockets[0].closed is False
    client.close()
    assert sockets[0].closed is True


def test_httpx_transport_preserves_host_header_and_uses_pinned_socket():
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family, [_response()])
        sockets.append(connection)
        return connection

    transport = PinnedHTTPTransport(
        resolver=lambda host, port: [_record("93.184.216.34")],
        socket_factory=make_socket,
    )
    response = transport.handle_request(httpx.Request("GET", "http://events.example.test/event"))
    assert response.read() == b"ok"
    assert sockets[0].connected_to == ("93.184.216.34", 80)
    assert b"Host: events.example.test" in b"".join(sockets[0].sent)
    response.close()
    transport.close()


def test_redirect_resolves_and_pins_each_allowed_hostname(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    tls = FakeTLSContext()
    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", lambda: tls)
    answers = {
        "events.example.test": "93.184.216.34",
        "calendar.example.test": "2606:4700:4700::1111",
    }
    resolved = []
    sockets = []
    response_data = [
        _response(status=302, headers=b"Location: https://calendar.example.test/final\r\n"),
        _response(body=b"done"),
    ]

    def resolver(host, port):
        resolved.append(host)
        return [_record(answers[host])]

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family, [response_data[len(sockets)]])
        sockets.append(connection)
        return connection

    with BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(resolver=resolver, socket_factory=make_socket),
        min_request_interval=0,
    ) as client:
        assert client.get("https://events.example.test/start") == b"done"

    assert resolved == ["events.example.test", "calendar.example.test"]
    assert sockets[0].connected_to == ("93.184.216.34", 443)
    assert sockets[1].connected_to == ("2606:4700:4700::1111", 443, 0, 0)
    assert tls.server_names == ["events.example.test", "calendar.example.test"]


def test_redirect_to_host_with_prohibited_dns_answer_is_rejected(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    tls = FakeTLSContext()
    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", lambda: tls)
    sockets = []

    def resolver(host, port):
        return [_record("93.184.216.34" if host == "events.example.test" else "127.0.0.1")]

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(
            family,
            [_response(status=302, headers=b"Location: https://calendar.example.test/final\r\n")],
        )
        sockets.append(connection)
        return connection

    with BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(resolver=resolver, socket_factory=make_socket),
        min_request_interval=0,
    ) as client:
        with pytest.raises(FetchError, match="network retry exhausted"):
            client.get("https://events.example.test/start")
    assert len(sockets) == 1


def test_retry_revalidates_dns_and_reconnects_to_new_numeric_answer(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", FakeTLSContext)
    resolutions = []
    sockets = []
    waits = []

    def resolver(host, port):
        address = "93.184.216.34" if not resolutions else "1.1.1.1"
        resolutions.append((host, address))
        return [_record(address)]

    def make_socket(family, _socktype, _proto):
        if not sockets:
            connection = FakeSocket(family, connect_error=TimeoutError("temporary"))
        else:
            connection = FakeSocket(family, [_response()])
        sockets.append(connection)
        return connection

    with BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(resolver=resolver, socket_factory=make_socket),
        max_retries=1,
        min_request_interval=0,
        sleep=waits.append,
    ) as client:
        assert client.get("https://events.example.test/retry") == b"ok"

    assert resolutions == [
        ("events.example.test", "93.184.216.34"),
        ("events.example.test", "1.1.1.1"),
    ]
    assert [connection.connected_to for connection in sockets] == [
        ("93.184.216.34", 443),
        ("1.1.1.1", 443),
    ]
    assert waits == [0.5]


def test_keepalive_reuses_only_the_already_pinned_connection(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", FakeTLSContext)
    resolutions = []
    sockets = []

    def resolver(host, port):
        resolutions.append(host)
        return [_record("93.184.216.34")]

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family, [_response(body=b"one"), _response(body=b"two")])
        sockets.append(connection)
        return connection

    with BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(resolver=resolver, socket_factory=make_socket),
        min_request_interval=0,
    ) as client:
        assert client.get("https://events.example.test/one") == b"one"
        assert client.get("https://events.example.test/two") == b"two"

    assert resolutions == ["events.example.test"]
    assert len(sockets) == 1
    assert sockets[0].connected_to == ("93.184.216.34", 443)


def test_environment_proxy_settings_do_not_replace_pinned_backend(monkeypatch):
    import app.discovery.pinned_transport as pinned_transport

    monkeypatch.setattr(pinned_transport.httpcore, "default_ssl_context", FakeTLSContext)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    sockets = []

    def make_socket(family, _socktype, _proto):
        connection = FakeSocket(family, [_response()])
        sockets.append(connection)
        return connection

    with BoundedHttpClient(
        {"example.test"},
        transport=PinnedHTTPTransport(
            resolver=lambda host, port: [_record("93.184.216.34")],
            socket_factory=make_socket,
        ),
        min_request_interval=0,
    ) as client:
        assert client.get("https://events.example.test/proxy") == b"ok"

    assert sockets[0].connected_to == ("93.184.216.34", 443)
    assert b"Host: events.example.test" in b"".join(sockets[0].sent)


def test_existing_mock_transport_injection_remains_supported():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"fixture"))
    with BoundedHttpClient({"example.test"}, transport=transport, min_request_interval=0) as client:
        assert client.get("https://events.example.test/fixture") == b"fixture"


def test_default_client_uses_pinned_transport_without_trusting_environment(monkeypatch):
    client_options = {}
    original_client = httpx.Client

    def recording_client(*args, **kwargs):
        client_options.update(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", recording_client)
    client = BoundedHttpClient({"example.test"}, min_request_interval=0)
    try:
        assert isinstance(client_options["transport"], PinnedHTTPTransport)
        assert client_options["trust_env"] is False
    finally:
        client.close()
