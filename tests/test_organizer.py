from datetime import datetime, timezone

import pytest

from hermes_dmhy_anime_subscription.config import OrganizerConfig
from hermes_dmhy_anime_subscription.models import OrganizerMode
from hermes_dmhy_anime_subscription.monitor import OrganizerInput
from hermes_dmhy_anime_subscription.organizer import _primary_title_alias, organize_media

NOW = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def test_dry_run_plans_media_server_layout_without_mutating_source(tmp_path):
    source = tmp_path / "downloads" / "[Subs] Example Show - 01 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 01 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    destination = library / "Example Show" / "Season 01" / "Example Show - S01E01 - Subs [1080p].mkv"
    assert result.actions[0].status == "planned"
    assert result.actions[0].destination_path == destination
    assert source.exists()
    assert not destination.exists()


def test_apply_copies_single_file_under_library_root(tmp_path):
    source = tmp_path / "downloads" / "[Subs] Example Show - 02 [720p].mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")

    library = tmp_path / "library"
    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 02 [720p]"),
        OrganizerConfig(mode=OrganizerMode.APPLY, library_root=library, staging_root=tmp_path / "staging"),
    )

    destination = library / "Example Show" / "Season 01" / "Example Show - S01E02 - Subs [720p].mp4"
    assert result.actions[0].status == "applied"
    assert destination.read_bytes() == b"video"
    assert source.read_bytes() == b"video"


def test_path_traversal_title_is_sanitized_inside_library_root(tmp_path):
    source = tmp_path / "downloads" / "[Bad] .. Evil - 03 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Bad] ../../Evil - 03 [1080p]", metadata={"series_title": "../../Evil"}),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    destination = result.actions[0].destination_path
    assert destination is not None
    assert destination.resolve(strict=False).is_relative_to(library.resolve(strict=False))
    assert ".." not in destination.relative_to(library).parts


def test_existing_destination_is_conflict_and_not_overwritten(tmp_path):
    source = tmp_path / "downloads" / "[Subs] Example Show - 04 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"new")
    library = tmp_path / "library"
    destination = library / "Example Show" / "Season 01" / "Example Show - S01E04 - Subs [1080p].mkv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 04 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.MOVE, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert result.actions[0].status == "conflict"
    assert result.events[0].event_type == "organizer_conflict"
    assert destination.read_bytes() == b"existing"
    assert source.read_bytes() == b"new"


def test_multifile_torrent_ignores_extras_and_preserves_subtitles(tmp_path):
    source = tmp_path / "downloads" / "torrent"
    source.mkdir(parents=True)
    main = source / "[Subs] Example Show - 05 [1080p].mkv"
    sample = source / "[Subs] Example Show - 05 sample [1080p].mkv"
    trailer = source / "trailer.mp4"
    subtitle = source / "[Subs] Example Show - 05 [1080p].ass"
    main.write_bytes(b"main-video")
    sample.write_bytes(b"sample-video")
    trailer.write_bytes(b"trailer-video")
    subtitle.write_text("subtitle", encoding="utf-8")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 05 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.MOVE, library_root=library, staging_root=tmp_path / "staging"),
    )

    destinations = {action.destination_path for action in result.actions}
    video_destination = library / "Example Show" / "Season 01" / "Example Show - S01E05 - Subs [1080p].mkv"
    subtitle_destination = library / "Example Show" / "Season 01" / "Example Show - S01E05 - Subs [1080p].ass"
    assert destinations == {video_destination, subtitle_destination}
    assert video_destination.read_bytes() == b"main-video"
    assert subtitle_destination.read_text(encoding="utf-8") == "subtitle"
    assert sample.exists()
    assert trailer.exists()


def test_multifile_torrent_uses_each_file_episode_when_title_has_episode(tmp_path):
    source = tmp_path / "downloads" / "torrent"
    source.mkdir(parents=True)
    first = source / "Anime - 01.mkv"
    second = source / "Anime - 02.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Anime - 01 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert {action.destination_path for action in result.actions} == {
        library / "Anime" / "Season 01" / "Anime - S01E01 - Subs [1080p].mkv",
        library / "Anime" / "Season 01" / "Anime - S01E02 - Subs [1080p].mkv",
    }


def test_multifile_torrent_keeps_release_title_season_for_episode_only_stems(tmp_path):
    source = tmp_path / "downloads" / "torrent"
    source.mkdir(parents=True)
    first = source / "Dr.STONE - 01.mkv"
    second = source / "Dr.STONE - 02.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(
            source,
            title="[ANi] Dr.STONE S04 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
        ),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert {action.destination_path for action in result.actions} == {
        library / "Dr STONE" / "Season 04" / "Dr STONE - S04E01 - ANi [1080P].mkv",
        library / "Dr STONE" / "Season 04" / "Dr STONE - S04E02 - ANi [1080P].mkv",
    }


def test_single_video_directory_prefers_release_title_episode_for_numeric_series(tmp_path):
    source = tmp_path / "downloads" / "torrent"
    source.mkdir(parents=True)
    video = source / "86 - Eighty Six.mkv"
    video.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] 86 - Eighty Six - 01 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert result.actions[0].destination_path == library / "86 Eighty Six" / "Season 01" / "86 Eighty Six - S01E01 - Subs [1080p].mkv"


def test_single_video_directory_keeps_title_season_when_stem_has_episode(tmp_path):
    source = tmp_path / "downloads" / "torrent"
    source.mkdir(parents=True)
    video = source / "Anime - 01.mkv"
    video.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Anime S04 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert result.actions[0].destination_path == library / "Anime" / "Season 04" / "Anime - S04E01 - Subs [1080p].mkv"


def test_season_only_title_without_episode_does_not_fabricate_episode_from_season(tmp_path):
    source = tmp_path / "downloads" / "release.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show Season 2 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert result.actions[0].status == "unsorted"
    assert result.actions[0].episode is None
    assert result.actions[0].season == 2


def test_bangumi_chinese_title_uses_flat_series_directory(tmp_path):
    source = tmp_path / "downloads" / "frieren"
    source.mkdir(parents=True)
    video = source / "[Subs] Frieren Beyond Journeys End - 07 [1080p].mkv"
    video.write_bytes(b"video")
    subtitle = video.with_suffix(".ass")
    subtitle.write_text("subtitle", encoding="utf-8")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Frieren Beyond Journeys End - 07 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: "葬送的芙莉莲",
    )

    destinations = {action.destination_path for action in result.actions}
    assert destinations == {
        library / "葬送的芙莉莲" / "Frieren Beyond Journeys End - S01E07 - Subs [1080p].mkv",
        library / "葬送的芙莉莲" / "Frieren Beyond Journeys End - S01E07 - Subs [1080p].ass",
    }


def test_bangumi_lookup_without_chinese_title_keeps_season_layout(tmp_path):
    source = tmp_path / "downloads" / "[Subs] Frieren Beyond Journeys End - 08 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Frieren Beyond Journeys End - 08 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: None,
    )

    assert result.actions[0].destination_path == library / "Frieren Beyond Journeys End" / "Season 01" / "Frieren Beyond Journeys End - S01E08 - Subs [1080p].mkv"


def test_episode_title_after_number_is_not_kept_in_series_title(tmp_path):
    source = tmp_path / "downloads" / "release.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 01 - The Beginning [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == ["Example Show"]
    assert result.actions[0].episode == 1
    assert result.actions[0].destination_path == library / "Example Show" / "Season 01" / "Example Show - S01E01 - Subs [1080p].mkv"


def test_bangumi_lookup_uses_season_aware_release_title_for_s02(tmp_path):
    source = tmp_path / "downloads" / "[Subs] Example Show S02E03 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show S02E03 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or "示例 第二季",
    )

    assert calls == ["[Subs] Example Show S02E03 [1080p]"]
    assert result.actions[0].destination_path == library / "示例 第二季" / "Example Show - S02E03 - Subs [1080p].mkv"


@pytest.mark.parametrize("separator", [" / ", "/", " /", "/ "])
def test_bangumi_lookup_uses_primary_alias_for_slash_separated_release_titles(
    tmp_path, separator
):
    source = tmp_path / "downloads" / "[DMG&SumiSora&LoliHouse] Tongari Boushi no Atelier - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(
            source,
            title=f"[DMG&SumiSora&LoliHouse] Tongari Boushi no Atelier{separator}尖帽子的魔法工房 - 08 [WebRip 1080p HEVC-10bit AAC ASSx2]",
        ),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or "尖帽子的魔法工房",
    )

    assert calls == ["Tongari Boushi no Atelier"]
    assert result.actions[0].destination_path == library / "尖帽子的魔法工房" / "Tongari Boushi no Atelier 尖帽子的魔法工房 - S01E08 - DMG&SumiSora&LoliHouse [1080p].mkv"


def test_bangumi_lookup_preserves_canonical_slash_title_for_lookup(tmp_path):
    source = tmp_path / "downloads" / "[Subs] Fate stay night - 01 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    organize_media(
        _organizer_input(source, title="[Subs] Fate/stay night - 01 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == ["Fate/stay night"]


@pytest.mark.parametrize(
    "release_title",
    [
        "[Subs] Some English Title / Alternate Romaji - 01 [1080p]",
        "[Subs] Some English Title /Alternate Romaji - 01 [1080p]",
        "[Subs] Some English Title/ Alternate Romaji - 01 [1080p]",
    ],
)
def test_bangumi_lookup_splits_spaced_latin_slash_alias_for_lookup(
    tmp_path, release_title
):
    source = tmp_path / "downloads" / "[Subs] Some English Title Alternate Romaji - 01 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    organize_media(
        _organizer_input(source, title=release_title),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == ["Some English Title"]


@pytest.mark.parametrize(
    "release_title",
    [
        "[Subs] Some Title / Fate/stay night - 01 [1080p]",
        "[Subs] Some Title / Alt/Other - 01 [1080p]",
    ],
)
def test_bangumi_lookup_splits_spaced_alias_before_slashy_alternate(
    tmp_path, release_title
):
    source = tmp_path / "downloads" / "[Subs] Some Title - 01 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    organize_media(
        _organizer_input(source, title=release_title),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == ["Some Title"]


@pytest.mark.parametrize(
    ("release_title", "expected_lookup"),
    [
        ("[Subs] Fate/stay night / Unlimited Blade Works - 01 [1080p]", "Fate/stay night"),
        ("[Subs] http://host//path / Alias - 01 [1080p]", "http://host//path"),
    ],
)
def test_bangumi_lookup_preserves_slashy_left_alias_before_spaced_separator(
    tmp_path, release_title, expected_lookup
):
    source = tmp_path / "downloads" / "[Subs] Slash Alias - 01 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    organize_media(
        _organizer_input(source, title=release_title),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == [expected_lookup]


def test_bangumi_lookup_preserves_path_like_slash_continuation(tmp_path):
    source = tmp_path / "downloads" / "[Subs] path mnt downloads Anime - 01 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(source, title="[Subs] path /mnt/downloads Anime - 01 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == ["path /mnt/downloads Anime"]
    assert result.actions[0].destination_path == library / "path mnt downloads Anime" / "Season 01" / "path mnt downloads Anime - S01E01 - Subs [1080p].mkv"


@pytest.mark.parametrize(
    ("release_title", "expected_lookup", "expected_destination"),
    [
        (
            "[ANi] Anime Title / 動畫標題 - 01 [1080p]",
            "Anime Title",
            "Anime Title 動畫標題 - S01E01 - ANi [1080p].mkv",
        ),
        (
            "[G] Tongari Boushi no Atelier/尖帽子的魔法工房 - 08 [1080p]",
            "Tongari Boushi no Atelier",
            "Tongari Boushi no Atelier 尖帽子的魔法工房 - S01E08 - G [1080p].mkv",
        ),
        (
            "[G] Gundam G no Reconguista - 01 [1080p]",
            "Gundam G no Reconguista",
            "Gundam G no Reconguista - S01E01 - G [1080p].mkv",
        ),
        (
            "[G] G no Reconguista - 01 [1080p]",
            "G no Reconguista",
            "G no Reconguista - S01E01 - G [1080p].mkv",
        ),
    ],
)
def test_bangumi_lookup_preserves_title_words_containing_release_group_token(
    tmp_path, release_title, expected_lookup, expected_destination
):
    source = tmp_path / "downloads" / "release.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(source, title=release_title),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == [expected_lookup]
    expected_series_dir = expected_destination.split(" - S", 1)[0]
    assert result.actions[0].destination_path == library / expected_series_dir / "Season 01" / expected_destination


@pytest.mark.parametrize(
    ("release_title", "expected_series"),
    [
        ("G no Reconguista - 01 [1080p]", "G no Reconguista"),
        ("[1080p] G no Reconguista - 01", "G no Reconguista"),
        ("G-Gundam - 01 [1080p]", "G Gundam"),
    ],
)
def test_single_character_metadata_release_group_does_not_remove_title_initial(
    tmp_path, release_title, expected_series
):
    source = tmp_path / "downloads" / "release.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(source, title=release_title, metadata={"release_group": "G", "quality": "1080p"}),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == [expected_series]
    assert result.actions[0].destination_path == library / expected_series / "Season 01" / f"{expected_series} - S01E01 - G [1080p].mkv"


@pytest.mark.parametrize(
    ("release_title", "expected_destination"),
    [
        (
            "[Subs] 86 - Eighty Six - 01 [1080p]",
            "86 Eighty Six - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] 2.5-jigen no Ririsa - 01 [1080p]",
            "2 5 jigen no Ririsa - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Anime - 08 [1080p AAC 2.0]",
            "Anime - S01E08 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Anime - 08 [1080p AAC 2]",
            "Anime - S01E08 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Anime - 08 [AAC 2]",
            "Anime - S01E08 - Subs [Unknown].mkv",
        ),
        (
            "[Subs] Anime - 01 [1080p][CHT][10-bit]",
            "Anime - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Anime [01 1080p]",
            "Anime - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Anime - 01 of 12 [1080p]",
            "Anime - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Anime - 01-02 [1080p]",
            "Anime - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Anime - 01_02 [1080p]",
            "Anime - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Example Show Season 2 - 01 [1080p]",
            "Example Show - S02E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Example Show 2nd Season - 01 [1080p]",
            "Example Show - S02E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Example Show 第2期 - 01 [1080p]",
            "Example Show - S02E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Example.Show.01.1080p",
            "Example Show - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Example.Show.01.1080P",
            "Example Show - S01E01 - Subs [1080P].mkv",
        ),
        (
            "[Subs] Example.Show.01.4K",
            "Example Show - S01E01 - Subs [4K].mkv",
        ),
    ],
)
def test_numeric_title_tokens_are_not_parsed_as_episode_numbers(tmp_path, release_title, expected_destination):
    source = tmp_path / "downloads" / "release.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title=release_title),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    expected_series_dir = expected_destination.split(" - S", 1)[0]
    expected_season = int(expected_destination.split(" - S", 1)[1][:2])
    assert result.actions[0].destination_path == library / expected_series_dir / f"Season {expected_season:02d}" / expected_destination


def test_season_only_release_notation_sets_season_and_removes_season_from_title(tmp_path):
    source = tmp_path / "downloads" / "release.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(
            source,
            title="[ANi] Dr.STONE S04 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
        ),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert result.actions[0].destination_path == library / "Dr STONE" / "Season 04" / "Dr STONE - S04E01 - ANi [1080P].mkv"


@pytest.mark.parametrize("source_name", ["download-123.mkv", "[Other] Anime - 02 [1080p].mkv", "[Subs] Anime - 02 [1080p].mkv"])
def test_release_title_episode_takes_precedence_over_numeric_source_stem(tmp_path, source_name):
    source = tmp_path / "downloads" / source_name
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Anime - 08 [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert result.actions[0].destination_path == library / "Anime" / "Season 01" / "Anime - S01E08 - Subs [1080p].mkv"


@pytest.mark.parametrize(
    ("release_title", "metadata", "expected_destination"),
    [
        (
            "[1080p] Subs Anime - 01",
            {"release_group": "Subs", "quality": "1080p"},
            "Anime - S01E01 - Subs [1080p].mkv",
        ),
        (
            "Anime - Subs - 01 [1080p]",
            {"release_group": "Subs", "quality": "1080p"},
            "Anime - S01E01 - Subs [1080p].mkv",
        ),
        (
            "[Subs] Example Show - 01 1080p",
            {},
            "Example Show - S01E01 - Subs [1080p].mkv",
        ),
    ],
)
def test_release_group_token_is_removed_when_not_a_matching_leading_bracket(
    tmp_path, release_title, metadata, expected_destination
):
    source = tmp_path / "downloads" / "release.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title=release_title, metadata=metadata),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    expected_series_dir = expected_destination.split(" - S", 1)[0]
    assert result.actions[0].destination_path == library / expected_series_dir / "Season 01" / expected_destination


@pytest.mark.parametrize(
    "title",
    [
        "path /mnt Anime",
        "Alpha /mnt/尖帽子",
        "/mnt/downloads Anime",
        "/mnt/尖帽子",
        "/home Anime",
        "/opt Anime",
    ],
)
def test_primary_title_alias_preserves_absolute_path_like_continuations(title):
    assert _primary_title_alias(title) == title


@pytest.mark.parametrize(
    ("release_title", "expected_lookup"),
    [
        ("[Subs] Some English Title / - 01 [1080p]", "Some English Title"),
        ("[Subs] / Alternate Romaji - 01 [1080p]", "Alternate Romaji"),
        ("[Subs] Alpha / / Beta - 01 [1080p]", "Alpha"),
        ("[Subs] Alpha // Beta - 01 [1080p]", "Alpha"),
        ("[Subs] / / Beta - 01 [1080p]", "Beta"),
        ("[Subs] Alpha//Beta - 01 [1080p]", "Alpha"),
        ("[Subs] //Beta - 01 [1080p]", "Beta"),
        ("[Subs] Alpha// - 01 [1080p]", "Alpha"),
    ],
)
def test_bangumi_lookup_ignores_empty_slash_alias_segments(
    tmp_path, release_title, expected_lookup
):
    source = tmp_path / "downloads" / "[Subs] Some English Title - 01 [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    organize_media(
        _organizer_input(source, title=release_title),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or None,
    )

    assert calls == [expected_lookup]


@pytest.mark.parametrize(
    ("title", "expected_alias"),
    [
        ("http://host//path", "http://host//path"),
        ("https://host/path//file", "https://host/path//file"),
        ("Title http://host//path", "Title http://host//path"),
        ("Alpha//Beta", "Alpha"),
        ("Fate/stay night // Unlimited Blade Works", "Fate/stay night"),
        ("Fate/stay night//Unlimited Blade Works", "Fate/stay night"),
        ("//Beta", "Beta"),
        ("Alpha//", "Alpha"),
    ],
)
def test_primary_title_alias_preserves_urlish_titles(title, expected_alias):
    assert _primary_title_alias(title) == expected_alias


def test_unparsed_episode_falls_back_to_unsorted_with_warning_event(tmp_path):
    source = tmp_path / "downloads" / "[Subs] Example OVA [1080p].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example OVA [1080p]"),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
    )

    assert result.actions[0].status == "unsorted"
    assert result.actions[0].destination_path == library / "_Unsorted" / "Example OVA" / "Example OVA.mkv"
    assert result.events[0].event_type == "organizer_unsorted"


def _organizer_input(source, title="Example", metadata=None):
    return OrganizerInput(
        job_id="job-1",
        torrent_hash="hash",
        title=title,
        source_path=str(source),
        completed_at=NOW,
        metadata=metadata or {},
    )
