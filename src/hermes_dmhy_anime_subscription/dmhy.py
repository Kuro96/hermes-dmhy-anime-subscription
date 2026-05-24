"""DMHY RSS URL building and fixture-friendly parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
import xml.etree.ElementTree as ET

from .models import FeedItem

DMHY_RSS_BASE_URL = "https://share.dmhy.org/topics/rss/rss.xml"
DMHY_RSS_ROOT = "https://share.dmhy.org/topics/rss"
SEASON_PACK_SORT_ID = "31"
SEASON_PACK_CATEGORY_CLUES = (
    "季度",
    "全集",
    "合集",
    "季度全集",
    "season pack",
    "batch",
    "complete",
)


@dataclass(frozen=True, slots=True)
class RssParseError:
    message: str
    item_index: int | None = None
    title: str | None = None
    guid: str | None = None
    recoverable: bool = True


@dataclass(frozen=True, slots=True)
class RssParseResult:
    items: tuple[FeedItem, ...]
    errors: tuple[RssParseError, ...] = ()


class DmhyRssClient:
    """Small stdlib-only helper for DMHY RSS URLs and XML parsing."""

    def build_url(
        self,
        *,
        keyword: str | None = None,
        team_id: int | str | None = None,
        sort_id: int | str | None = None,
        user_id: int | str | None = None,
    ) -> str:
        return build_rss_url(keyword=keyword, team_id=team_id, sort_id=sort_id, user_id=user_id)

    def parse(self, xml_text: str | bytes, *, source_feed: str | None = None) -> RssParseResult:
        return parse_rss(xml_text, source_feed=source_feed)


def build_rss_url(
    *,
    keyword: str | None = None,
    team_id: int | str | None = None,
    sort_id: int | str | None = None,
    user_id: int | str | None = None,
) -> str:
    selectors = {"team_id": team_id, "sort_id": sort_id, "user_id": user_id}
    selected = [(name, value) for name, value in selectors.items() if value is not None]
    if len(selected) > 1:
        raise ValueError("Only one DMHY feed selector may be provided")
    if selected and keyword is not None:
        raise ValueError("keyword cannot be combined with team_id, sort_id, or user_id")
    if keyword is not None:
        keyword_value = keyword.strip()
        if not keyword_value:
            raise ValueError("keyword must not be empty")
        return f"{DMHY_RSS_BASE_URL}?keyword={quote(keyword_value)}"
    if selected:
        selector_name, selector_value = selected[0]
        selector_text = _selector_value(selector_value, selector_name)
        return f"{DMHY_RSS_ROOT}/{selector_name}/{selector_text}/rss.xml"
    return DMHY_RSS_BASE_URL


def parse_rss_file(path: str | Path, *, source_feed: str | None = None) -> RssParseResult:
    return parse_rss(Path(path).read_text(encoding="utf-8"), source_feed=source_feed)


def parse_rss(xml_text: str | bytes, *, source_feed: str | None = None) -> RssParseResult:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return RssParseResult(items=(), errors=(RssParseError(message=f"Invalid RSS XML: {exc}", recoverable=True),))

    found_channel = _first_child(root, "channel")
    channel = found_channel if found_channel is not None else root
    items: list[FeedItem] = []
    errors: list[RssParseError] = []
    for index, item_element in enumerate(_children(channel, "item")):
        parsed_item, error = _parse_item(item_element, index, source_feed)
        if parsed_item is not None:
            items.append(parsed_item)
        if error is not None:
            errors.append(error)
    return RssParseResult(items=tuple(items), errors=tuple(errors))


def extract_info_hash(magnet_uri: str | None) -> str | None:
    if not magnet_uri:
        return None
    parsed = urlparse(magnet_uri)
    if parsed.scheme.lower() != "magnet":
        return None
    xt_values = parse_qs(parsed.query).get("xt", ())
    for value in xt_values:
        prefix = "urn:btih:"
        if value.lower().startswith(prefix):
            info_hash = value[len(prefix) :].strip()
            return info_hash.lower() or None
    return None


def _parse_item(item_element: ET.Element, index: int, source_feed: str | None) -> tuple[FeedItem | None, RssParseError | None]:
    title = _text(item_element, "title") or ""
    link = _text(item_element, "link") or ""
    guid = _text(item_element, "guid")
    description = _text(item_element, "description")
    author = _text(item_element, "author")
    category = _text(item_element, "category")
    magnet_uri = _enclosure_url(item_element)
    info_hash = extract_info_hash(magnet_uri)
    error: RssParseError | None = None
    if not magnet_uri or not info_hash:
        error = RssParseError(
            message="RSS item is missing an enclosure magnet URI with a btih infohash",
            item_index=index,
            title=title or None,
            guid=guid,
            recoverable=True,
        )
        return None, error
    item = FeedItem(
        title=title,
        link=link,
        published_at=_parse_pubdate(_text(item_element, "pubDate")),
        guid=guid,
        info_hash=info_hash,
        magnet_uri=magnet_uri,
        normalized_title=title.strip().casefold() or None,
        source_feed=source_feed,
        description=description,
        author=author,
        category=category,
        is_season_pack=_is_season_pack(category, link, description, title),
    )
    return item, error


def _parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _is_season_pack(category: str | None, link: str, description: str | None, title: str) -> bool:
    haystack = " ".join(part for part in (category, link, description, title) if part).casefold()
    return "sort_id=31" in haystack or any(clue.casefold() in haystack for clue in SEASON_PACK_CATEGORY_CLUES)


def _selector_value(value: int | str, label: str) -> str:
    text = str(value).strip()
    if not text or not text.isdecimal():
        raise ValueError(f"{label} must be a positive integer")
    if int(text) < 1:
        raise ValueError(f"{label} must be a positive integer")
    return text


def _enclosure_url(item_element: ET.Element) -> str | None:
    enclosure = _first_child(item_element, "enclosure")
    if enclosure is None:
        return None
    value = enclosure.attrib.get("url")
    if value and value.strip():
        return value.strip()
    return None


def _text(element: ET.Element, child_name: str) -> str | None:
    child = _first_child(element, child_name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _first_child(element: ET.Element, child_name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == child_name:
            return child
    return None


def _children(element: ET.Element, child_name: str) -> tuple[ET.Element, ...]:
    return tuple(child for child in element if _local_name(child.tag) == child_name)


def _local_name(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1]
