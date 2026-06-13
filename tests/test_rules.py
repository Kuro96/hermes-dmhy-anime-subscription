from pathlib import Path

from hermes_dmhy_anime_subscription.dmhy import parse_rss, parse_rss_file
from hermes_dmhy_anime_subscription.models import FeedItem, RuleEpisodeMode, SubscriptionRule
from hermes_dmhy_anime_subscription.rules import dedupe_items, evaluate_rule


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "dmhy"


def test_default_episode_rule_accepts_anime_fixture_and_rejects_pack_fixture():
    anime = parse_rss_file(FIXTURE_DIR / "rss-anime.xml", source_feed="anime").items[0]
    pack = parse_rss_file(FIXTURE_DIR / "rss-season-pack.xml", source_feed="anime").items[0]
    rule = SubscriptionRule(
        name="example-anime",
        include_keywords=("Example Anime",),
        team_names=("ExampleSub",),
        resolutions=("1080p",),
        categories=("動畫",),
    )

    accepted = evaluate_rule(anime, rule)
    rejected = evaluate_rule(pack, rule)

    assert accepted.accepted is True
    assert accepted.reasons == ("accepted",)
    assert accepted.candidate is not None
    assert accepted.candidate.rule_name == "example-anime"
    assert rejected.accepted is False
    assert "pack_not_allowed" in rejected.reasons


def test_allow_packs_or_pack_mode_accepts_season_pack_fixture():
    pack = parse_rss_file(FIXTURE_DIR / "rss-season-pack.xml", source_feed="anime").items[0]

    allow_packs_result = evaluate_rule(
        pack,
        SubscriptionRule(
            name="pack-allowed",
            include_keywords=("Example Anime",),
            categories=("季度全集",),
            allow_packs=True,
        ),
    )
    pack_mode_result = evaluate_rule(
        pack,
        SubscriptionRule(
            name="pack-mode",
            include_keywords=("Example Anime",),
            episode_mode=RuleEpisodeMode.PACK,
        ),
    )

    assert allow_packs_result.accepted is True
    assert pack_mode_result.accepted is True


def test_pack_mode_accepts_description_only_episode_range_pack():
    item = parse_rss(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[Subs] Example Show 01-12 [1080p]</title>
      <link>https://share.dmhy.org/topics/view/200112_example_show_01_12.html</link>
      <description>BD合集</description>
      <author>Subs</author>
      <category>動畫</category>
      <guid>season-pack-range-description-only</guid>
      <enclosure url="magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345679" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
""",
        source_feed="anime",
    ).items[0]

    result = evaluate_rule(
        item,
        SubscriptionRule(
            name="pack-mode",
            include_keywords=("Example Show",),
            episode_mode=RuleEpisodeMode.PACK,
        ),
    )

    assert item.is_season_pack is True
    assert result.accepted is True


def test_dedupe_accepts_first_item_and_skips_same_infohash_or_guid():
    duplicate_items = parse_rss_file(FIXTURE_DIR / "rss-duplicate.xml", source_feed="duplicate").items
    guid_a = FeedItem(title="Guid A", link="l", guid="same-guid")
    guid_b = FeedItem(title="Guid B", link="l", guid="same-guid")

    infohash_decisions = dedupe_items(duplicate_items)
    guid_decisions = dedupe_items((guid_a, guid_b))

    assert [decision.accepted for decision in infohash_decisions] == [True, False]
    assert infohash_decisions[0].reason == "first_seen"
    assert infohash_decisions[1].reason == "duplicate"
    assert infohash_decisions[1].duplicate_of is duplicate_items[0]
    assert [decision.accepted for decision in guid_decisions] == [True, False]
    assert guid_decisions[1].dedupe_key == "guid:same-guid"


def test_excluded_resolution_team_keyword_and_disabled_record_explicit_reasons():
    item = parse_rss_file(FIXTURE_DIR / "rss-anime.xml", source_feed="anime").items[0]

    result = evaluate_rule(
        item,
        SubscriptionRule(
            name="rejecting-rule",
            include_keywords=("Example Anime",),
            exclude_keywords=("CHS",),
            team_names=("OtherSub",),
            resolutions=("720p",),
            categories=("季度全集",),
        ),
    )
    disabled_result = evaluate_rule(item, SubscriptionRule(name="disabled", enabled=False))

    assert result.accepted is False
    assert "exclude_keyword" in result.reasons
    assert "team_mismatch" in result.reasons
    assert "resolution_mismatch" in result.reasons
    assert "category_mismatch" in result.reasons
    assert disabled_result.reasons == ("disabled",)


def test_regex_keywords_can_accept_and_reject_titles():
    item = parse_rss_file(FIXTURE_DIR / "rss-anime.xml", source_feed="anime").items[0]
    result = evaluate_rule(
        item,
        SubscriptionRule(
            name="regex-rule",
            include_keywords=(r"Example\s+Anime\s+-\s+\d+",),
            exclude_keywords=(r"\[720p\]",),
            use_regex=True,
        ),
    )

    assert result.accepted is True
