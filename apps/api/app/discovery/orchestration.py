"""Authorized, bounded source fetching and source-grounded extraction orchestration."""

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import httpx

from app.discovery.extraction import ExtractionObservation
from app.discovery.http import BoundedHttpClient
from app.discovery.ollama import OllamaExtractionClient
from app.discovery.registry import RegistryError, authorize_source_execution
from app.discovery.urls import validate_source_url

_MAX_FETCH_BYTES = 2_000_000
_MAX_EXTRACTED_TEXT_CHARACTERS = 100_000
_SUPPORTED_HTML_METHODS = frozenset({"html", "structured_html"})
_HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template"})
_BLOCK_TAGS = frozenset(
    {"address", "article", "aside", "blockquote", "br", "div", "footer", "h1", "h2",
     "h3", "h4", "h5", "h6", "header", "li", "main", "ol", "p", "section", "table",
     "td", "th", "tr", "ul"}
)

@dataclass(frozen=True, slots=True)
class AuthorizedExtractionResult:
    """Unpersisted observations and the registry source context they came from."""

    registry_source_id: str
    source_url: str
    publisher_name: str
    observations: dict[str, ExtractionObservation]


class SourceContentError(RuntimeError):
    """Fetched content could not be safely decoded as useful HTML text."""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0
        self.saw_tag = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.saw_tag = True
        if tag in _HIDDEN_TAGS:
            self._hidden_depth += 1
        elif self._hidden_depth == 0 and tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.saw_tag = True
        if self._hidden_depth == 0 and tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        self.saw_tag = True
        if tag in _HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1
        elif self._hidden_depth == 0 and tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0:
            self.parts.append(data)


def _extract_visible_text(document: bytes, max_characters: int) -> str:
    try:
        html = document.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceContentError("source HTML is not valid UTF-8") from exc
    parser = _VisibleTextParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError) as exc:
        raise SourceContentError("source HTML could not be parsed") from exc
    text = " ".join("".join(parser.parts).split())
    if not parser.saw_tag:
        raise SourceContentError("source response does not contain HTML markup")
    if not text:
        raise SourceContentError("source HTML contains no visible text")
    if len(text) > max_characters:
        raise SourceContentError("extracted source text exceeds size limit")
    return text


class AuthorizedSourceExtraction:
    """Fetch one explicitly authorized HTML source and return validated observations."""

    def __init__(
        self,
        ollama_client: OllamaExtractionClient,
        *,
        registry_path: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
        max_extracted_text_characters: int = _MAX_EXTRACTED_TEXT_CHARACTERS,
    ) -> None:
        if not 0 < max_extracted_text_characters <= _MAX_EXTRACTED_TEXT_CHARACTERS:
            raise ValueError("max_extracted_text_characters is outside the allowed range")
        self.ollama_client = ollama_client
        self.registry_path = registry_path
        self.transport = transport
        self.max_extracted_text_characters = max_extracted_text_characters

    def extract(self, registry_source_id: str, source_url: str) -> AuthorizedExtractionResult:
        """Authorize, fetch bounded HTML, then run the validated Ollama extraction."""
        source = authorize_source_execution(registry_source_id, self.registry_path)
        if source.id != registry_source_id:
            raise RegistryError("authorized source identity does not match request")
        if source.access["method"] not in _SUPPORTED_HTML_METHODS:
            raise RegistryError(
                f"source {registry_source_id} access method is not enabled for HTML extraction"
            )
        allowed_hosts = set(source.access["allowed_hosts"])
        safe_url = validate_source_url(source_url, allowed_hosts)

        response_limit = min(source.limits["max_response_bytes"], _MAX_FETCH_BYTES)
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
        with BoundedHttpClient(
            allowed_hosts,
            timeout=timeout,
            max_bytes=response_limit,
            max_redirects=0,
            max_retries=0,
            min_request_interval=source.limits["min_request_interval_s"],
            transport=self.transport,
        ) as fetcher:
            document = fetcher.get(safe_url)

        source_text = _extract_visible_text(
            document, self.max_extracted_text_characters
        )
        observations: dict[str, ExtractionObservation] = self.ollama_client.extract(
            source_text
        )
        return AuthorizedExtractionResult(
            registry_source_id=source.id,
            source_url=safe_url,
            publisher_name=source.publisher_name,
            observations=observations,
        )
