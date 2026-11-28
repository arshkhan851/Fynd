"""HTTPX transport that validates DNS answers and pins direct TCP connections."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable, Iterable, Iterator
from itertools import islice
from typing import Any

import httpcore
import httpx

ResolverRecord = tuple[int, int, int, str, tuple[Any, ...]]
Resolver = Callable[[str, int], Iterable[ResolverRecord]]
SocketFactory = Callable[[int, int, int], socket.socket]
MAX_DNS_ANSWERS = 16  # Allows normal dual-stack/CDN sets while bounding fallback attempts.
_IPV4_SPECIAL_PREFIXES = (ipaddress.ip_network("192.88.99.0/24"),)
_IPV6_TRANSLATION_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _system_resolver(host: str, port: int) -> list[ResolverRecord]:
    return socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )


def _public_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv4Address) and any(
        address in prefix for prefix in _IPV4_SPECIAL_PREFIXES
    ):
        raise ValueError("DNS returned a prohibited IPv4 address")
    if isinstance(address, ipaddress.IPv6Address):
        if (
            address.scope_id is not None
            or address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
            or any(address in prefix for prefix in _IPV6_TRANSLATION_PREFIXES)
        ):
            raise ValueError("DNS returned a prohibited IPv6 address")
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise ValueError("DNS returned a non-public address")
    return address


def _map_httpcore_exception(exc: Exception) -> Exception:
    mappings: tuple[tuple[type[Exception], type[Exception]], ...] = (
        (httpcore.ConnectTimeout, httpx.ConnectTimeout),
        (httpcore.ConnectError, httpx.ConnectError),
        (httpcore.ReadTimeout, httpx.ReadTimeout),
        (httpcore.ReadError, httpx.ReadError),
        (httpcore.WriteTimeout, httpx.WriteTimeout),
        (httpcore.WriteError, httpx.WriteError),
        (httpcore.PoolTimeout, httpx.PoolTimeout),
        (httpcore.ProxyError, httpx.ProxyError),
        (httpcore.ConnectionNotAvailable, httpx.NetworkError),
        (httpcore.LocalProtocolError, httpx.LocalProtocolError),
        (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
        (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
        (httpcore.ProtocolError, httpx.ProtocolError),
        (httpcore.NetworkError, httpx.NetworkError),
        (httpcore.TimeoutException, httpx.TimeoutException),
    )
    for source_type, target_type in mappings:
        if isinstance(exc, source_type):
            return target_type(str(exc))
    return exc


class _PinnedSocketStream(httpcore.NetworkStream):
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            self._connection.settimeout(timeout)
            return self._connection.recv(max_bytes)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout("socket read timed out") from exc
        except OSError as exc:
            raise httpcore.ReadError("socket read failed") from exc

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            self._connection.settimeout(timeout)
            self._connection.sendall(buffer)
        except TimeoutError as exc:
            raise httpcore.WriteTimeout("socket write timed out") from exc
        except OSError as exc:
            raise httpcore.WriteError("socket write failed") from exc

    def close(self) -> None:
        self._connection.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> _PinnedSocketStream:
        try:
            self._connection.settimeout(timeout)
            tls_connection = ssl_context.wrap_socket(
                self._connection,
                server_hostname=server_hostname,
            )
        except TimeoutError as exc:
            self.close()
            raise httpcore.ConnectTimeout("TLS handshake timed out") from exc
        except (OSError, ssl.SSLError) as exc:
            self.close()
            raise httpcore.ConnectError("TLS handshake failed") from exc
        return _PinnedSocketStream(tls_connection)

    def get_extra_info(self, info: str) -> Any:
        if info == "ssl_object":
            return self._connection if isinstance(self._connection, ssl.SSLSocket) else None
        if info == "client_addr":
            return self._connection.getsockname()
        if info == "server_addr":
            return self._connection.getpeername()
        if info == "socket":
            return self._connection
        return None


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Resolve and validate every answer before connecting to a numeric IP."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self._resolver = resolver or _system_resolver
        self._socket_factory = socket_factory or socket.socket

    def _resolve(self, host: str, port: int) -> list[tuple[int, int, int, tuple[Any, ...]]]:
        try:
            records = list(islice(self._resolver(host, port), MAX_DNS_ANSWERS + 1))
        except Exception as exc:
            raise httpcore.ConnectError("DNS resolution failed") from exc
        if not records:
            raise httpcore.ConnectError("DNS returned no addresses")
        if len(records) > MAX_DNS_ANSWERS:
            raise httpcore.ConnectError(f"DNS returned more than {MAX_DNS_ANSWERS} addresses")

        validated: list[tuple[int, int, int, tuple[Any, ...]]] = []
        try:
            for family, socktype, proto, _canonname, sockaddr in records:
                if family not in (socket.AF_INET, socket.AF_INET6):
                    raise ValueError("DNS returned an unsupported address family")
                if socktype != socket.SOCK_STREAM:
                    raise ValueError("DNS returned an unsupported socket type")
                address = _public_address(sockaddr[0])
                if family == socket.AF_INET and address.version != 4:
                    raise ValueError("DNS address does not match its address family")
                if family == socket.AF_INET6 and address.version != 6:
                    raise ValueError("DNS address does not match its address family")
                numeric_sockaddr: tuple[Any, ...]
                if family == socket.AF_INET6:
                    numeric_sockaddr = (str(address), port, 0, 0)
                else:
                    numeric_sockaddr = (str(address), port)
                validated.append((family, socktype, proto, numeric_sockaddr))
        except (TypeError, ValueError, IndexError) as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        return validated

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        endpoints = self._resolve(host, port)
        last_error: OSError | None = None

        for family, socktype, proto, sockaddr in endpoints:
            connection: socket.socket | None = None
            try:
                connection = self._socket_factory(family, socktype, proto)
                connection.settimeout(timeout)
                if local_address is not None:
                    local_ip = ipaddress.ip_address(local_address)
                    if local_ip.version != (4 if family == socket.AF_INET else 6):
                        raise ValueError("local address does not match destination family")
                    if family == socket.AF_INET6:
                        bind_address = (str(local_ip), 0, 0, 0)
                    else:
                        bind_address = (str(local_ip), 0)
                    connection.bind(bind_address)
                for option in socket_options or ():
                    connection.setsockopt(*option)
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.connect(sockaddr)
                return _PinnedSocketStream(connection)
            except TimeoutError as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
            except OSError as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
            except ValueError as exc:
                if connection is not None:
                    connection.close()
                raise httpcore.ConnectError(str(exc)) from exc

        if isinstance(last_error, TimeoutError):
            raise httpcore.ConnectTimeout(
                "connection to validated address timed out"
            ) from last_error
        raise httpcore.ConnectError("connection to validated addresses failed") from last_error

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Unix sockets are not supported by the discovery transport")


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self._response = response

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._response.iter_stream()
        except Exception as exc:
            mapped = _map_httpcore_exception(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def close(self) -> None:
        self._response.close()


class PinnedHTTPTransport(httpx.BaseTransport):
    """HTTPX sync transport backed by a DNS-validating httpcore connection pool."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpcore.default_ssl_context(),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=PinnedNetworkBackend(
                resolver=resolver,
                socket_factory=socket_factory,
            ),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            core_response = self._pool.handle_request(core_request)
        except Exception as exc:
            mapped = _map_httpcore_exception(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_ResponseStream(core_response),
            extensions=core_response.extensions,
        )

    def close(self) -> None:
        self._pool.close()
