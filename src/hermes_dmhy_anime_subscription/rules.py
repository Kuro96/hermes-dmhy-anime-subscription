"""Subscription rule matching and feed-item dedupe decisions."""

from __future__ import annotations

from dataclasses import dataclass
from re import Pattern
import re

from .models import FeedItem, ReleaseCandidate, RuleEpisodeMode, SubscriptionRule


@dataclass(frozen=True, slots=True)
class RuleMatchResult:
    item: FeedItem
    rule: SubscriptionRule
    accepted: bool
    reasons: tuple[str, ...]
    candidate: ReleaseCandidate | None = None


@dataclass(frozen=True, slots=True)
class DedupeDecision:
    item: FeedItem
    accepted: bool
    dedupe_key: str
    reason: str
    duplicate_of: FeedItem | None = None


def evaluate_rule(item: FeedItem, rule: SubscriptionRule) -> RuleMatchResult:
    reasons = _rejection_reasons(item, rule)
    if reasons:
        return RuleMatchResult(item=item, rule=rule, accepted=False, reasons=tuple(reasons))
    candidate = ReleaseCandidate(
        feed_item=item,
        rule_name=rule.name,
        title=item.title,
        quality=_matched_resolution(item, rule),
        priority=rule.priority,
        reason="accepted",
    )
    return RuleMatchResult(item=item, rule=rule, accepted=True, reasons=("accepted",), candidate=candidate)


def match_rules(item: FeedItem, rules: tuple[SubscriptionRule, ...]) -> tuple[RuleMatchResult, ...]:
    return tuple(evaluate_rule(item, rule) for rule in rules)


def dedupe_items(items: tuple[FeedItem, ...] | list[FeedItem]) -> tuple[DedupeDecision, ...]:
    seen: dict[str, FeedItem] = {}
    decisions: list[DedupeDecision] = []
    for item in items:
        key = item.dedupe_key
        first_item = seen.get(key)
        if first_item is None:
            seen[key] = item
            decisions.append(DedupeDecision(item=item, accepted=True, dedupe_key=key, reason="first_seen"))
        else:
            decisions.append(DedupeDecision(item=item, accepted=False, dedupe_key=key, reason="duplicate", duplicate_of=first_item))
    return tuple(decisions)


def _rejection_reasons(item: FeedItem, rule: SubscriptionRule) -> list[str]:
    reasons: list[str] = []
    if not rule.enabled:
        reasons.append("disabled")
        return reasons
    if rule.feed_names and (item.source_feed is None or item.source_feed not in rule.feed_names):
        reasons.append("feed_name_mismatch")
    if item.is_season_pack and not _allows_pack(rule):
        reasons.append("pack_not_allowed")
    if not item.is_season_pack and rule.episode_mode is RuleEpisodeMode.PACK:
        reasons.append("episode_not_allowed")
    if rule.include_keywords:
        missing = [keyword for keyword in rule.include_keywords if not _matches(keyword, _item_text(item), rule.use_regex)]
        if missing:
            reasons.append("include_keyword_missing")
    if any(_matches(keyword, _item_text(item), rule.use_regex) for keyword in rule.exclude_keywords):
        reasons.append("exclude_keyword")
    if rule.team_names and not any(_matches(team_name, _team_text(item), rule.use_regex) for team_name in rule.team_names):
        reasons.append("team_mismatch")
    if rule.resolutions and _matched_resolution(item, rule) is None:
        reasons.append("resolution_mismatch")
    if rule.categories and not _matches_any_category(item, rule):
        reasons.append("category_mismatch")
    return reasons


def _allows_pack(rule: SubscriptionRule) -> bool:
    return rule.allow_packs or rule.episode_mode in {RuleEpisodeMode.PACK, RuleEpisodeMode.BOTH}


def _matched_resolution(item: FeedItem, rule: SubscriptionRule) -> str | None:
    if not rule.resolutions:
        return None
    text = _item_text(item)
    for resolution in rule.resolutions:
        if _matches(resolution, text, rule.use_regex):
            return resolution
    return None


def _matches_any_category(item: FeedItem, rule: SubscriptionRule) -> bool:
    category = (item.category or "").strip().casefold()
    return any(category == configured.strip().casefold() for configured in rule.categories)


def _matches(pattern: str, text: str, use_regex: bool) -> bool:
    if use_regex:
        return re.search(_compiled(pattern), text) is not None
    return pattern.strip().casefold() in text.casefold()


def _compiled(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _item_text(item: FeedItem) -> str:
    return " ".join(part for part in (item.title, item.description, item.category) if part)


def _team_text(item: FeedItem) -> str:
    return " ".join(part for part in (item.author, item.title) if part)
