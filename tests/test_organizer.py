from datetime import datetime, timezone

import pytest

from hermes_dmhy_anime_subscription.config import OrganizerConfig
from hermes_dmhy_anime_subscription.models import OrganizerMode
from hermes_dmhy_anime_subscription.monitor import OrganizerInput
from hermes_dmhy_anime_subscription.organizer import EpisodeParserResult, organize_media

NOW = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def test_simple_release_title_plans_media_server_destination(tmp_path):
    source = _video(tmp_path, "[Subs] Example Show - 01 [1080p].mkv")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 01 [1080p]"),
        _config(tmp_path, library),
    )

    assert result.actions[0].status == "planned"
    assert result.actions[0].destination_path == library / "Example Show" / "Season 01" / "Example Show - S01E01 - Subs [1080p].mkv"
    assert source.exists()


@pytest.mark.parametrize(
    ("release_title", "expected_path"),
    [
        ("Example Show S02E03", "Example Show/Season 02/Example Show - S02E03 - Unknown [Unknown].mkv"),
        ("Example Show S02 - 03", "Example Show/Season 02/Example Show - S02E03 - Unknown [Unknown].mkv"),
        ("Example Show Season 2 - 03", "Example Show/Season 02/Example Show - S02E03 - Unknown [Unknown].mkv"),
        ("Example Show 第2季 第03話", "Example Show/Season 02/Example Show - S02E03 - Unknown [Unknown].mkv"),
        ("[Subs][Example Show][01][1080p]", "Example Show/Season 01/Example Show - S01E01 - Subs [1080p].mkv"),
    ],
)
def test_high_confidence_supported_filename_shapes_plan_destination(tmp_path, release_title, expected_path):
    source = _video(tmp_path, "release.mkv")
    library = tmp_path / "library"

    result = organize_media(_organizer_input(source, title=release_title), _config(tmp_path, library))

    assert result.actions[0].status == "planned"
    assert result.actions[0].destination_path == library / expected_path


def test_cjk_season_destination_is_preserved(tmp_path):
    source = _video(tmp_path, "[字幕组] 示例动画 第2季 第03話 [1080p].mkv")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[字幕组] 示例动画 第2季 第03話 [1080p]"),
        _config(tmp_path, library),
    )

    assert result.actions[0].destination_path == library / "示例动画" / "Season 02" / "示例动画 - S02E03 - 字幕组 [1080p].mkv"


def test_numeric_title_is_preserved_as_series_title(tmp_path):
    source = _video(tmp_path, "release.mkv")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] 86 - Eighty Six - 01 [1080p]"),
        _config(tmp_path, library),
    )

    assert result.actions[0].destination_path == library / "86 Eighty Six" / "Season 01" / "86 Eighty Six - S01E01 - Subs [1080p].mkv"


@pytest.mark.parametrize(
    "release_title",
    [
        "[Subs] Example Show - 01-12 [1080p]",
        "[Subs] Example Show S02 E01-E12 [1080p]",
        "[Subs] Example Show Season 2 Part 2 - 03 [1080p]",
        "[Subs] Example Show Season 2 Cour 2 [1080p]",
        "[Subs] Example Show Season 2 Vol. 3 [1080p]",
        "[Subs] Example Show BD 2 Discs - 01 [1080p]",
        "[Subs][01][02][1080p]",
    ],
)
def test_ambiguous_complex_and_numeric_only_names_are_unsorted(tmp_path, release_title):
    source = _video(tmp_path, "release.mkv")
    library = tmp_path / "library"

    result = organize_media(_organizer_input(source, title=release_title), _config(tmp_path, library))

    assert result.actions[0].status == "unsorted"
    assert result.actions[0].episode is None
    assert result.events[0].event_type == "organizer_unsorted"


def test_no_agent_fallback_for_unparsed_episode_is_unsorted(tmp_path):
    source = _video(tmp_path, "[Subs] Example OVA [1080p].mkv")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example OVA [1080p]"),
        _config(tmp_path, library),
    )

    assert result.actions[0].status == "unsorted"
    assert result.actions[0].destination_path == library / "_Unsorted" / "Example OVA" / "Example OVA.mkv"
    assert result.events[0].event_type == "organizer_unsorted"


def test_injected_episode_parser_plans_otherwise_unsupported_title(tmp_path):
    source = _video(tmp_path, "[Subs] Example OVA [1080p].mkv")
    library = tmp_path / "library"
    calls = []

    def episode_parser(text):
        calls.append(text)
        if text == "[Subs] Example OVA [1080p]":
            return EpisodeParserResult(series_title="Example OVA", episode=13)
        return None

    result = organize_media(
        _organizer_input(source, title="[Subs] Example OVA [1080p]"),
        _config(tmp_path, library),
        episode_parser=episode_parser,
    )

    assert calls == ["[Subs] Example OVA [1080p]"]
    assert result.actions[0].status == "planned"
    assert result.actions[0].destination_path == library / "Example OVA" / "Season 01" / "Example OVA - S01E13 - Subs [1080p].mkv"


@pytest.mark.parametrize(
    ("release_title", "expected_calls", "expected_path"),
    [
        (
            "[ANi] Example Show [01][1080P]",
            ["[ANi] Example Show [01][1080P]"],
            "Example Show/Season 01/Example Show - S01E01 - ANi [1080P].mkv",
        ),
        (
            "[ANi] Example Show - 01 [1080P][Baha][WEB-DL]",
            ["[ANi] Example Show - 01 [1080P][Baha][WEB-DL]"],
            "Example Show/Season 01/Example Show - S01E01 - ANi [1080P].mkv",
        ),
        (
            "[Example Show][01][1080p]",
            ["[Example Show][01][1080p]"],
            "Example Show/Season 01/Example Show - S01E01 - Unknown [1080p].mkv",
        ),
    ],
)
def test_agent_fallback_handles_reviewed_bracket_cases_without_new_private_regex(tmp_path, release_title, expected_calls, expected_path):
    source = _video(tmp_path, "release.mkv")
    library = tmp_path / "library"
    calls = []

    def episode_parser(text):
        calls.append(text)
        if text == "[Example Show][01][1080p]":
            return EpisodeParserResult(series_title="Example Show", episode=1, release_group="", quality="1080p")
        if text in {"[ANi] Example Show [01][1080P]", "[ANi] Example Show - 01 [1080P][Baha][WEB-DL]"}:
            return EpisodeParserResult(series_title="Example Show", episode=1)
        return None

    result = organize_media(
        _organizer_input(source, title=release_title),
        _config(tmp_path, library),
        episode_parser=episode_parser,
    )

    assert calls == expected_calls
    assert result.actions[0].status == "planned"
    assert result.actions[0].destination_path == library / expected_path


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
        _config(tmp_path, library, mode=OrganizerMode.MOVE),
    )

    destinations = {action.destination_path for action in result.actions}
    video_destination = library / "Example Show" / "Season 01" / "Example Show - S01E05 - Subs [1080p].mkv"
    subtitle_destination = library / "Example Show" / "Season 01" / "Example Show - S01E05 - Subs [1080p].ass"
    assert destinations == {video_destination, subtitle_destination}
    assert video_destination.read_bytes() == b"main-video"
    assert subtitle_destination.read_text(encoding="utf-8") == "subtitle"
    assert sample.exists()
    assert trailer.exists()


def test_path_traversal_title_is_sanitized_inside_library_root(tmp_path):
    source = _video(tmp_path, "[Bad] Evil - 03 [1080p].mkv")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Bad] Evil - 03 [1080p]", metadata={"series_title": "../../Evil"}),
        _config(tmp_path, library),
    )

    destination = result.actions[0].destination_path
    assert destination is not None
    assert destination.resolve(strict=False).is_relative_to(library.resolve(strict=False))
    assert ".." not in destination.relative_to(library).parts


def test_existing_destination_is_conflict_and_not_overwritten(tmp_path):
    source = _video(tmp_path, "[Subs] Example Show - 04 [1080p].mkv", b"new")
    library = tmp_path / "library"
    destination = library / "Example Show" / "Season 01" / "Example Show - S01E04 - Subs [1080p].mkv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 04 [1080p]"),
        _config(tmp_path, library, mode=OrganizerMode.MOVE),
    )

    assert result.actions[0].status == "conflict"
    assert result.events[0].event_type == "organizer_conflict"
    assert destination.read_bytes() == b"existing"
    assert source.read_bytes() == b"new"


def test_apply_copies_file_without_mutating_source(tmp_path):
    source = _video(tmp_path, "[Subs] Example Show - 02 [720p].mp4", b"video")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show - 02 [720p]"),
        _config(tmp_path, library, mode=OrganizerMode.APPLY),
    )

    destination = library / "Example Show" / "Season 01" / "Example Show - S01E02 - Subs [720p].mp4"
    assert result.actions[0].status == "applied"
    assert destination.read_bytes() == b"video"
    assert source.read_bytes() == b"video"


def test_bangumi_lookup_injects_flat_library_title_and_uses_lookup_alias(tmp_path):
    source = _video(tmp_path, "[Subs] Example Show S02E03 [1080p].mkv")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(source, title="[Subs] Example Show S02E03 [1080p]"),
        _config(tmp_path, library),
        bangumi_lookup=lambda title: calls.append(title) or "示例 第二季",
    )

    assert calls == ["Example Show"]
    assert result.actions[0].destination_path == library / "示例 第二季" / "示例 第二季 - S02E03 - Subs [1080p].mkv"


def test_bangumi_lookup_does_not_bypass_unsorted_without_episode(tmp_path):
    source = _video(tmp_path, "[Subs] Example OVA [1080p].mkv")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[Subs] Example OVA [1080p]"),
        _config(tmp_path, library),
        bangumi_lookup=lambda _title: "示例 OVA",
    )

    assert result.actions[0].status == "unsorted"
    assert result.actions[0].destination_path == library / "_Unsorted" / "示例 OVA" / "示例 OVA.mkv"


def test_multifile_torrent_uses_each_file_episode_and_release_title_season(tmp_path):
    source = tmp_path / "downloads" / "torrent"
    source.mkdir(parents=True)
    (source / "Dr.STONE - 01.mkv").write_bytes(b"first")
    (source / "Dr.STONE - 02.mkv").write_bytes(b"second")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[ANi] Dr.STONE S04 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"),
        _config(tmp_path, library),
    )

    assert {action.destination_path for action in result.actions} == {
        library / "Dr STONE" / "Season 04" / "Dr STONE - S04E01 - ANi [1080P].mkv",
        library / "Dr STONE" / "Season 04" / "Dr STONE - S04E02 - ANi [1080P].mkv",
    }


def test_multifile_torrent_preserves_numeric_stems_with_release_title_series(tmp_path):
    source = tmp_path / "downloads" / "torrent"
    source.mkdir(parents=True)
    (source / "01.mkv").write_bytes(b"first-video")
    (source / "02.mkv").write_bytes(b"second-vide")
    library = tmp_path / "library"

    result = organize_media(
        _organizer_input(source, title="[ANi] Example Show [1080P]"),
        _config(tmp_path, library),
    )

    assert {action.destination_path for action in result.actions} == {
        library / "Example Show" / "Season 01" / "Example Show - S01E01 - ANi [1080P].mkv",
        library / "Example Show" / "Season 01" / "Example Show - S01E02 - ANi [1080P].mkv",
    }


def _video(tmp_path, name, content=b"video"):
    source = tmp_path / "downloads" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _config(tmp_path, library, mode=OrganizerMode.DRY_RUN):
    return OrganizerConfig(mode=mode, library_root=library, staging_root=tmp_path / "staging")


def _organizer_input(source, title="Example", metadata=None):
    return OrganizerInput(
        job_id="job-1",
        torrent_hash="hash",
        title=title,
        source_path=str(source),
        completed_at=NOW,
        metadata=metadata or {},
    )
