from datetime import datetime, timezone

from hermes_dmhy_anime_subscription.config import OrganizerConfig
from hermes_dmhy_anime_subscription.models import OrganizerMode
from hermes_dmhy_anime_subscription.monitor import OrganizerInput
from hermes_dmhy_anime_subscription.organizer import organize_media

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


def test_bangumi_lookup_uses_primary_alias_for_slash_separated_release_titles(tmp_path):
    source = tmp_path / "downloads" / "[DMG&SumiSora&LoliHouse] Tongari Boushi no Atelier - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    library = tmp_path / "library"
    calls = []

    result = organize_media(
        _organizer_input(
            source,
            title="[DMG&SumiSora&LoliHouse] Tongari Boushi no Atelier / 尖帽子的魔法工房 - 08 [WebRip 1080p HEVC-10bit AAC ASSx2]",
        ),
        OrganizerConfig(mode=OrganizerMode.DRY_RUN, library_root=library, staging_root=tmp_path / "staging"),
        bangumi_lookup=lambda title: calls.append(title) or "尖帽子的魔法工房",
    )

    assert calls == ["Tongari Boushi no Atelier"]
    assert result.actions[0].destination_path == library / "尖帽子的魔法工房" / "Tongari Boushi no Atelier 尖帽子的魔法工房 - S01E08 - DMG&SumiSora&LoliHouse [1080p].mkv"


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
