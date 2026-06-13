from datetime import timezone
from pathlib import Path

import pytest

from hermes_dmhy_anime_subscription.dmhy import DmhyRssClient, build_rss_url, extract_info_hash, parse_rss, parse_rss_file


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "dmhy"


def test_build_rss_url_variants_match_dmhy_shapes():
    assert build_rss_url() == "https://share.dmhy.org/topics/rss/rss.xml"
    assert build_rss_url(keyword="葬送 的 芙莉蓮") == "https://share.dmhy.org/topics/rss/rss.xml?keyword=%E8%91%AC%E9%80%81%20%E7%9A%84%20%E8%8A%99%E8%8E%89%E8%93%AE"
    assert build_rss_url(team_id=123) == "https://share.dmhy.org/topics/rss/team_id/123/rss.xml"
    assert build_rss_url(sort_id=2) == "https://share.dmhy.org/topics/rss/sort_id/2/rss.xml"
    assert build_rss_url(user_id=456) == "https://share.dmhy.org/topics/rss/user_id/456/rss.xml"


def test_build_rss_url_rejects_ambiguous_selectors():
    with pytest.raises(ValueError, match="Only one"):
        build_rss_url(team_id=1, sort_id=2)
    with pytest.raises(ValueError, match="keyword"):
        build_rss_url(keyword="example", user_id=3)


def test_parse_anime_fixture_extracts_rss_metadata_and_infohash():
    result = parse_rss_file(FIXTURE_DIR / "rss-anime.xml", source_feed="anime")

    assert result.errors == ()
    assert len(result.items) == 1
    item = result.items[0]
    assert item.title == "[ExampleSub] Example Anime - 01 [1080p][CHS]"
    assert item.link.endswith("100001_example_anime_01.html")
    assert item.guid == "https://share.dmhy.org/topics/view/100001_example_anime_01.html"
    assert item.info_hash == "abcdef1234567890abcdef1234567890abcdef12"
    assert item.magnet_uri is not None and item.magnet_uri.startswith("magnet:?")
    assert item.author == "ExampleSub"
    assert item.category == "動畫"
    assert item.description == "Example release description"
    assert item.source_feed == "anime"
    assert item.published_at is not None
    assert item.published_at.astimezone(timezone.utc).isoformat() == "2026-05-24T10:30:00+00:00"
    assert item.is_season_pack is False


def test_parse_season_pack_fixture_marks_pack_and_accepts_base32_infohash():
    result = parse_rss_file(FIXTURE_DIR / "rss-season-pack.xml", source_feed="season-pack")

    assert result.errors == ()
    item = result.items[0]
    assert item.info_hash == "mfrggzdfmztwq2lknnwg23tpoi"
    assert item.category == "季度全集"
    assert item.is_season_pack is True


def test_explicit_episode_title_is_not_pack_from_description_only_collection_words():
    result = parse_rss(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>六四位元字幕組★躲在超市後門抽菸的兩人 Super no Ura de Yani Suu Futari★04(abema先行版)★1920x1080★AVC AAC MP4★繁體中文(重要公告)</title>
      <link>https://share.dmhy.org/topics/view/200064_supermarket_yani_04.html</link>
      <description>重要公告：BD合集資訊請見字幕組網站。</description>
      <author>六四位元字幕組</author>
      <category>動畫</category>
      <guid>episode-with-description-collection-words</guid>
      <enclosure url="magnet:?xt=urn:btih:1234567890abcdef1234567890abcdef12345678" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
""",
        source_feed="anime",
    )

    assert result.errors == ()
    assert len(result.items) == 1
    assert result.items[0].is_season_pack is False


def test_episode_range_title_is_pack_from_description_only_collection_words():
    result = parse_rss(
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
    )

    assert result.errors == ()
    assert len(result.items) == 1
    assert result.items[0].is_season_pack is True


def test_parse_duplicate_fixture_preserves_duplicate_infohash_for_later_state_dedupe():
    result = parse_rss_file(FIXTURE_DIR / "rss-duplicate.xml", source_feed="duplicate")

    assert result.errors == ()
    assert len(result.items) == 2
    assert result.items[0].info_hash == result.items[1].info_hash
    assert result.items[0].dedupe_key == result.items[1].dedupe_key


def test_missing_enclosure_is_recoverable_and_skipped():
    result = parse_rss_file(FIXTURE_DIR / "rss-missing-enclosure.xml", source_feed="broken")

    assert result.items == ()
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.recoverable is True
    assert error.guid == "missing-enclosure-guid"
    assert "missing an enclosure magnet" in error.message


def test_non_actionable_manga_empty_enclosure_is_skipped_without_error():
    result = parse_rss(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>DMHY Keyword RSS</title>
    <item>
      <title>[OldMangaGroup] Example Manga Chapter 12</title>
      <link>https://share.dmhy.org/topics/view/123456_example_manga.html</link>
      <pubDate>Sun, 24 May 2026 10:30:00 +0000</pubDate>
      <description>Old manga entry from a keyword feed</description>
      <author>OldMangaGroup</author>
      <category>漫畫</category>
      <guid>https://share.dmhy.org/topics/view/123456_example_manga.html</guid>
      <enclosure url="" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
""",
        source_feed="keyword",
    )

    assert result.items == ()
    assert result.errors == ()


def test_anime_empty_enclosure_url_remains_parse_error():
    result = parse_rss(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[ExampleSub] Example Anime - 02 [1080p][CHS]</title>
      <category>動畫</category>
      <guid>anime-empty-enclosure-guid</guid>
      <enclosure url="" type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
""",
        source_feed="anime",
    )

    assert result.items == ()
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.guid == "anime-empty-enclosure-guid"
    assert "missing an enclosure magnet" in error.message


def test_manga_enclosure_without_url_attribute_remains_parse_error():
    result = parse_rss(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[OldMangaGroup] Example Manga Chapter 13</title>
      <category>漫畫</category>
      <guid>manga-missing-url-attribute-guid</guid>
      <enclosure type="application/x-bittorrent" />
    </item>
  </channel>
</rss>
""",
        source_feed="keyword",
    )

    assert result.items == ()
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.guid == "manga-missing-url-attribute-guid"
    assert "missing an enclosure magnet" in error.message


def test_invalid_rss_xml_remains_parse_error():
    result = parse_rss("<rss><channel><item></channel></rss>")

    assert result.items == ()
    assert len(result.errors) == 1
    assert "Invalid RSS XML" in result.errors[0].message


def test_client_wraps_url_builder_and_parser():
    client = DmhyRssClient()

    assert client.build_url(sort_id=31) == "https://share.dmhy.org/topics/rss/sort_id/31/rss.xml"
    assert client.parse((FIXTURE_DIR / "rss-anime.xml").read_text(encoding="utf-8")).items[0].author == "ExampleSub"


def test_extract_info_hash_treats_btih_value_as_opaque_lowercase():
    assert extract_info_hash("magnet:?xt=urn:btih:ABC123XYZ") == "abc123xyz"
    assert extract_info_hash("https://example.invalid/file.torrent") is None
