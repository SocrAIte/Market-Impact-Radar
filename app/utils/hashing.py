from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "cmpid"}


def stable_hash(value: str | bytes | None) -> str:
    if value is None:
        value = ""
    if isinstance(value, str):
        value = value.encode("utf-8", errors="ignore")
    return hashlib.sha256(value).hexdigest()


def normalize_text_for_hash(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.casefold()
    collapsed = re.sub(r"\s+", " ", lowered)
    return re.sub(r"[^\w\u4e00-\u9fff ]+", "", collapsed).strip()


def canonicalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        lowered = key.lower()
        if lowered in TRACKING_KEYS or any(lowered.startswith(prefix) for prefix in TRACKING_PREFIXES):
            continue
        query_pairs.append((key, value))
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted(query_pairs))
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, query, ""))
