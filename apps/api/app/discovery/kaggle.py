import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from app.discovery.candidates import DiscoveryCandidate
from app.discovery.registry import RegistryError, authorize_source_execution
from app.discovery.urls import UnsafeSourceUrl, validate_source_url

KAGGLE_ALLOWED_HOSTS = {"kaggle.com"}
KAGGLE_PUBLISHER_NAME = "Kaggle"


class KaggleDiscoveryError(RuntimeError):
    """Base error for Kaggle adapter failures."""


class KaggleAuthenticationError(KaggleDiscoveryError):
    pass


class KaggleRateLimitError(KaggleDiscoveryError):
    pass


class KaggleTimeoutError(KaggleDiscoveryError):
    pass


class KaggleMalformedResponseError(KaggleDiscoveryError):
    pass


@dataclass(frozen=True, slots=True)
class KaggleDiscoveryBatch:
    candidates: tuple[DiscoveryCandidate, ...]
    skipped: int
    pages_fetched: int


def _response_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _raise_api_error(exc: Exception) -> KaggleDiscoveryError:
    status = _response_status(exc)
    if status == 429:
        return KaggleRateLimitError("Kaggle rate limit reached; try again later")
    if status in {401, 403}:
        return KaggleAuthenticationError(
            "Kaggle rejected authentication; check the configured token or login"
        )
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return KaggleTimeoutError("Kaggle request timed out")
    if type(exc).__name__ in {"TimeoutException", "ReadTimeout", "ConnectTimeout"}:
        return KaggleTimeoutError("Kaggle request timed out")
    return KaggleDiscoveryError(f"Kaggle API request failed ({type(exc).__name__})")


class KaggleDiscoveryAdapter:
    """Fetch a bounded set of competition candidates with Kaggle's official client."""

    def __init__(
        self,
        *,
        api: Any | None = None,
        page_size: int = 20,
        max_pages: int = 3,
        result_limit: int = 50,
        registry_path: str | Path | None = None,
    ) -> None:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if max_pages < 1 or result_limit < 1:
            raise ValueError("max_pages and result_limit must be positive")
        if result_limit > page_size * max_pages:
            raise ValueError("result_limit cannot exceed page_size multiplied by max_pages")
        self.api = api
        self.page_size = page_size
        self.max_pages = max_pages
        self.result_limit = result_limit
        self.registry_path = registry_path

    @staticmethod
    def _default_api() -> Any:
        try:
            # Kaggle's package import and client construction may print auth setup guidance.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                from kaggle.api.kaggle_api_extended import KaggleApi

                return KaggleApi()
        except ImportError as exc:
            raise KaggleDiscoveryError(
                "Kaggle client is missing; install apps/api/requirements.txt"
            ) from exc
        except SystemExit as exc:
            raise KaggleAuthenticationError(
                "Kaggle credentials are missing or invalid; run `kaggle auth login` "
                "or configure KAGGLE_API_TOKEN"
            ) from exc
        except Exception as exc:
            raise _raise_api_error(exc) from exc

    @staticmethod
    def _authenticate(api: Any) -> None:
        authenticate = getattr(api, "authenticate", None)
        if authenticate is None:
            raise KaggleAuthenticationError("Kaggle client has no authentication method")
        try:
            # Kaggle's client prints setup guidance on missing credentials; surface a concise
            # message instead and keep any client output away from command output.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                authenticate()
        except SystemExit as exc:
            raise KaggleAuthenticationError(
                "Kaggle credentials are missing or invalid; run `kaggle auth login` "
                "or configure KAGGLE_API_TOKEN"
            ) from exc
        except Exception as exc:
            raise _raise_api_error(exc) from exc

    def discover(self) -> KaggleDiscoveryBatch:
        # The guard must run before client construction/authentication or any request.
        try:
            authorize_source_execution("kaggle", self.registry_path)
        except RegistryError as exc:
            raise KaggleDiscoveryError(str(exc)) from exc
        api = self.api if self.api is not None else self._default_api()
        self._authenticate(api)

        candidates: list[DiscoveryCandidate] = []
        skipped = 0
        pages_fetched = 0
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        for _ in range(self.max_pages):
            remaining = self.result_limit - len(candidates)
            if remaining <= 0:
                break
            try:
                # The official client can print verbose request headers, which may include
                # authentication material. Only the adapter summary/errors reach the console.
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    response = api.competitions_list(
                        group="general",
                        category="all",
                        sort_by="earliestDeadline",
                        page=-1,
                        page_size=min(self.page_size, remaining),
                        page_token=page_token,
                    )
            except Exception as exc:
                raise _raise_api_error(exc) from exc
            pages_fetched += 1
            rows = getattr(response, "competitions", None)
            if not isinstance(rows, (list, tuple)):
                raise KaggleMalformedResponseError(
                    "Kaggle returned a malformed competition list"
                )
            if not rows:
                break

            for row in rows:
                if len(candidates) >= self.result_limit:
                    break
                title = getattr(row, "title", None)
                event_url = getattr(row, "url", None)
                external_id = getattr(row, "ref", None)
                if not isinstance(title, str) or not title.strip():
                    skipped += 1
                    continue
                if not isinstance(event_url, str) or not event_url.strip():
                    skipped += 1
                    continue
                try:
                    validate_source_url(event_url, KAGGLE_ALLOWED_HOSTS)
                except UnsafeSourceUrl:
                    skipped += 1
                    continue
                if external_id is not None and (
                    not isinstance(external_id, str) or not external_id.strip()
                ):
                    external_id = None
                candidates.append(
                    DiscoveryCandidate(
                        title=title,
                        source_url=event_url,
                        publisher_name=KAGGLE_PUBLISHER_NAME,
                        source_kind="aggregator",
                        external_id=external_id,
                    )
                )

            next_token = getattr(response, "next_page_token", None)
            if next_token is None or next_token == "" or len(candidates) >= self.result_limit:
                break
            if (
                not isinstance(next_token, str)
                or not next_token.strip()
                or next_token in seen_page_tokens
            ):
                raise KaggleMalformedResponseError("Kaggle returned an invalid pagination token")
            seen_page_tokens.add(next_token)
            page_token = next_token

        return KaggleDiscoveryBatch(
            candidates=tuple(candidates),
            skipped=skipped,
            pages_fetched=pages_fetched,
        )
