import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.discovery.urls import validate_source_url


class FetchError(RuntimeError):
    pass


class BoundedHttpClient:
    def __init__(
        self,
        allowed_hosts: set[str],
        *,
        timeout: httpx.Timeout | None = None,
        max_bytes: int = 1_000_000,
        max_redirects: int = 3,
        max_retries: int = 2,
        min_request_interval: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            max_bytes <= 0
            or max_redirects < 0
            or max_retries < 0
            or min_request_interval < 0
        ):
            raise ValueError("limits must be non-negative and max_bytes must be positive")
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_retries = max_retries
        self.min_request_interval = min_request_interval
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            timeout=timeout or httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "FyndDiscovery/0.1 (+https://github.com/fynd)"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BoundedHttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _pace(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            delay = self.min_request_interval - (now - self._last_request_at)
            if delay > 0:
                self._sleep(delay)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                return max(0.0, (target - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def get(self, url: str) -> bytes:
        current_url = validate_source_url(url, self.allowed_hosts)
        redirects = 0
        retries = 0
        while True:
            self._pace()
            try:
                with self._client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        if redirects >= self.max_redirects:
                            raise FetchError("redirect limit exceeded")
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("redirect response has no location")
                        current_url = validate_source_url(
                            str(response.url.join(location)), self.allowed_hosts
                        )
                        redirects += 1
                        retries = 0
                        continue
                    if response.status_code in {429, 500, 502, 503, 504}:
                        if retries >= self.max_retries:
                            raise FetchError(
                                "retryable HTTP status exhausted: "
                                f"{response.status_code}"
                            )
                        wait = self._retry_after(response.headers.get("retry-after"))
                        if wait is not None and wait > 30:
                            raise FetchError("Retry-After exceeds the maximum retry wait")
                        self._sleep(wait if wait is not None else min(0.5 * (2**retries), 4.0))
                        retries += 1
                        continue
                    if response.status_code >= 400:
                        raise FetchError(f"HTTP status {response.status_code}")
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise FetchError("invalid Content-Length response header") from exc
                        if declared_length < 0:
                            raise FetchError("invalid Content-Length response header")
                        if declared_length > self.max_bytes:
                            raise FetchError("response exceeds configured size limit")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise FetchError("response exceeds configured size limit")
                    return bytes(body)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if retries >= self.max_retries:
                    raise FetchError(f"network retry exhausted: {type(exc).__name__}") from exc
                self._sleep(min(0.5 * (2**retries), 4.0))
                retries += 1
