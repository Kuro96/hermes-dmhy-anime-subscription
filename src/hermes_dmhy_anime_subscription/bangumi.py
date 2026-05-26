"""Bangumi title lookup using only the Python standard library."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

API_URL = "https://api.bgm.tv/v0/search/subjects"
DEFAULT_TIMEOUT_SECONDS = 5.0
USER_AGENT = "hermes-dmhy-anime-subscription/0.1 (+https://bangumi.tv)"

def lookup_chinese_title(title: str, *, opener: Callable[..., Any] = urlopen, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str | None:
    """Return the first Bangumi Chinese subject name for an anime title."""

    query = title.strip()
    if not query:
        return None
    payload = json.dumps(
        {
            "keyword": query,
            "sort": "match",
            "filter": {"type": [2]},
        }
    ).encode("utf-8")
    request = Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, TimeoutError, URLError):
        return None
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    data = decoded.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        chinese_title = item.get("name_cn")
        if isinstance(chinese_title, str) and chinese_title.strip():
            return chinese_title.strip()
    return None
