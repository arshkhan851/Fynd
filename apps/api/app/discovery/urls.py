import ipaddress
from urllib.parse import unquote_plus, urlsplit, urlunsplit


class UnsafeSourceUrl(ValueError):
    pass


_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _host_allowed(host: str, allowed_hosts: set[str]) -> bool:
    host = host.rstrip(".").lower()
    for allowed in allowed_hosts:
        allowed = allowed.rstrip(".").lower()
        if host == allowed or host.endswith(f".{allowed}"):
            return True
    return False


def validate_source_url(url: str, allowed_hosts: set[str]) -> str:
    """Validate a public HTTPS source URL against explicit exact/suffix host rules."""
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise UnsafeSourceUrl("URL must be a string without whitespace or control characters")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise UnsafeSourceUrl("malformed URL") from exc
    if parts.scheme.lower() != "https" or not host:
        raise UnsafeSourceUrl("source URL must use HTTPS and include a hostname")
    if parts.username is not None or parts.password is not None:
        raise UnsafeSourceUrl("credentials are not allowed in source URLs")
    if port not in (None, 443):
        raise UnsafeSourceUrl("non-standard ports are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise UnsafeSourceUrl("non-public IP addresses are not allowed")
    if not _host_allowed(host, allowed_hosts):
        raise UnsafeSourceUrl(f"host is not allowed: {host}")
    return url


def normalize_source_url(url: str, allowed_hosts: set[str]) -> str:
    validated = validate_source_url(url, allowed_hosts)
    parts = urlsplit(validated)
    host = (parts.hostname or "").lower().rstrip(".")
    query_items = []
    for item in parts.query.split("&") if parts.query else []:
        key = item.partition("=")[0]
        lowered = unquote_plus(key).lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        query_items.append(item)
    query = "&".join(query_items)
    return urlunsplit(("https", host, parts.path or "/", query, ""))
