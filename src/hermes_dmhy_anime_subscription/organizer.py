"""Safe filesystem organizer for completed anime downloads."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import OrganizerConfig
from .models import NotificationEvent, OrganizerMode
from .monitor import OrganizerInput

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".mov", ".m4v"})
SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".vtt"})
IGNORED_NAME_PARTS = frozenset({"sample", "extras", "extra", "trailer", "ncop", "nced"})
DEFAULT_SEASON = 1
BangumiLookup = Callable[[str], str | None]
PATH_LIKE_FIRST_SEGMENTS = frozenset({"mnt", "home", "opt", "var", "tmp", "usr", "etc", "media", "volumes", "downloads"})


@dataclass(frozen=True, slots=True)
class OrganizerAction:
    source_path: Path
    destination_path: Path | None
    status: str
    media_type: str
    reason: str | None = None
    episode: int | None = None
    season: int | None = None


@dataclass(frozen=True, slots=True)
class OrganizerResult:
    job_id: str
    mode: OrganizerMode
    actions: tuple[OrganizerAction, ...]
    events: tuple[NotificationEvent, ...] = field(default_factory=tuple)

    @property
    def planned_paths(self) -> tuple[Path, ...]:
        destinations: list[Path] = []
        for action in self.actions:
            if action.destination_path is not None:
                destinations.append(action.destination_path)
        return tuple(destinations)


@dataclass(frozen=True, slots=True)
class _EpisodeInfo:
    title: str
    lookup_title: str
    library_title: str
    flat_library: bool
    season: int
    episode: int | None
    release_group: str
    quality: str


def organize_media(organizer_input: OrganizerInput, config: OrganizerConfig, *, bangumi_lookup: BangumiLookup | None = None) -> OrganizerResult:
    """Plan or apply safe copies into a Jellyfin/Plex/Emby-compatible layout."""

    source_root = Path(organizer_input.source_path)
    library_root = config.library_root.resolve(strict=False)
    sources = _discover_sources(source_root)
    videos = _selected_video_files(sources)
    subtitles = _selected_subtitles(sources, videos)
    actions: list[OrganizerAction] = []
    infos: dict[Path, _EpisodeInfo] = {}
    bangumi_titles: dict[str, str | None] = {}

    for video in videos:
        info = _episode_info(
            video,
            organizer_input.title,
            organizer_input.metadata,
            prefer_stem_episode=len(videos) > 1,
        )
        info = _with_bangumi_title(info, bangumi_lookup, bangumi_titles)
        infos[video] = info
        destination = _video_destination(library_root, info, video.suffix)
        action = _plan_action(video, destination, library_root, config.mode, "video", info)
        if action.status == "planned" and config.mode in {OrganizerMode.APPLY, OrganizerMode.MOVE}:
            action = _apply_action(action)
        actions.append(action)

    for subtitle in subtitles:
        video = _matching_video(subtitle, videos) or videos[0] if videos else None
        if video is None:
            continue
        info = infos.get(video)
        if info is None:
            info = _episode_info(video, organizer_input.title, organizer_input.metadata)
            info = _with_bangumi_title(info, bangumi_lookup, bangumi_titles)
        destination = _subtitle_destination(library_root, info, video, subtitle)
        action = _plan_action(subtitle, destination, library_root, config.mode, "subtitle", info)
        if action.status == "planned" and config.mode in {OrganizerMode.APPLY, OrganizerMode.MOVE}:
            action = _apply_action(action)
        actions.append(action)

    if not actions:
        actions.append(
            OrganizerAction(
                source_path=source_root,
                destination_path=None,
                status="conflict",
                media_type="source",
                reason="No primary video files were found",
            )
        )

    return OrganizerResult(
        job_id=organizer_input.job_id,
        mode=config.mode,
        actions=tuple(actions),
        events=_events(organizer_input, actions),
    )


def _discover_sources(source_root: Path) -> tuple[Path, ...]:
    if source_root.is_file():
        return (source_root,)
    if not source_root.exists():
        return ()
    return tuple(path for path in source_root.rglob("*") if path.is_file())


def _selected_video_files(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    candidates = [path for path in paths if path.suffix.casefold() in VIDEO_EXTENSIONS and not _is_ignored_media(path)]
    return tuple(sorted(candidates, key=lambda path: (-_safe_size(path), str(path))))


def _selected_subtitles(paths: tuple[Path, ...], videos: tuple[Path, ...]) -> tuple[Path, ...]:
    if not videos:
        return ()
    video_stems = {video.stem.casefold() for video in videos}
    video_parents = {video.parent.resolve(strict=False) for video in videos}
    subtitles: list[Path] = []
    for path in paths:
        if path.suffix.casefold() not in SUBTITLE_EXTENSIONS:
            continue
        if path.stem.casefold() in video_stems or path.parent.resolve(strict=False) in video_parents:
            subtitles.append(path)
    return tuple(sorted(subtitles, key=lambda path: str(path)))


def _is_ignored_media(path: Path) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", path.stem.casefold())
    words = frozenset(normalized.split())
    return bool(words & IGNORED_NAME_PARTS)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _episode_info(
    path: Path, title: str, metadata: dict[str, object], *, prefer_stem_episode: bool = False
) -> _EpisodeInfo:
    text = f"{title} {path.stem}"
    title_season, title_episode = _parse_episode(title) if title else (DEFAULT_SEASON, None)
    stem_season, stem_episode = _parse_episode(path.stem)
    if stem_episode is not None and prefer_stem_episode:
        season = stem_season if stem_season != DEFAULT_SEASON else title_season
        episode = stem_episode
    elif stem_episode is not None and title_episode is None:
        season = title_season if title_season != DEFAULT_SEASON else stem_season
        episode = stem_episode
    else:
        season, episode = title_season, title_episode
    release_group = _metadata_text(metadata, "release_group") or _parse_release_group(title) or _parse_release_group(path.stem) or "Unknown"
    quality = _metadata_text(metadata, "quality") or _parse_quality(text) or "Unknown"
    series_episode = title_episode if title else stem_episode
    series_title = _metadata_text(metadata, "series_title") or _series_title(
        title, path.stem, release_group, quality, series_episode
    )
    return _EpisodeInfo(
        title=_sanitize_segment(series_title) or "Unknown Series",
        lookup_title=_lookup_title(title, path.stem, series_title),
        library_title=_sanitize_segment(series_title) or "Unknown Series",
        flat_library=False,
        season=season,
        episode=episode,
        release_group=_sanitize_segment(release_group) or "Unknown",
        quality=_sanitize_segment(quality) or "Unknown",
    )


def _with_bangumi_title(info: _EpisodeInfo, bangumi_lookup: BangumiLookup | None, cache: dict[str, str | None]) -> _EpisodeInfo:
    if bangumi_lookup is None:
        return info
    if info.lookup_title not in cache:
        try:
            cache[info.lookup_title] = bangumi_lookup(info.lookup_title)
        except Exception:
            cache[info.lookup_title] = None
    chinese_title = cache[info.lookup_title]
    if not chinese_title:
        return info
    library_title = _sanitize_segment(chinese_title)
    if not library_title:
        return info
    return replace(info, title=library_title, library_title=library_title, flat_library=True)


def _parse_episode(text: str) -> tuple[int, int | None]:
    e_prefixed_season_range = _season_context_e_prefixed_episode_range(text)
    if e_prefixed_season_range is not None:
        return e_prefixed_season_range, None
    season_episode = re.search(r"\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})\b", text, flags=re.IGNORECASE)
    if season_episode:
        return int(season_episode.group("season")), int(season_episode.group("episode"))
    season_range = _season_context_episode_range(text)
    if season_range is not None:
        return season_range, None
    season_then_episode = re.search(
        r"\bS(?P<season>\d{1,2})\b[\s_.-]+(?:E\s*)?(?P<episode>\d{1,3})(?:v\d+)?(?:\s*[-_]\s*\d{1,3})?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
        text,
        flags=re.IGNORECASE,
    )
    if season_then_episode:
        return int(season_then_episode.group("season")), int(season_then_episode.group("episode"))
    for pattern in (
        r"\bSeason\s*(?P<season>\d{1,2})\b[\s_.-]+(?:E\s*)?(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
        r"\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+Season\b[\s_.-]+(?:E\s*)?(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
        r"第\s*(?P<season>\d{1,2})\s*[季期][\s_.-]*(?:E\s*)?(?:第\s*)?(?P<episode>\d{1,3})(?:v\d+)?(?:\s*[話话集]|(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b)))",
    ):
        season_word_episode = re.search(pattern, text, flags=re.IGNORECASE)
        if season_word_episode:
            return int(season_word_episode.group("season")), int(season_word_episode.group("episode"))
    season_only_season = None
    season_only_span = None
    for pattern in (
        r"\bS(?P<season>\d{1,2})\b",
        r"\bSeason\s*(?P<season>\d{1,2})\b",
        r"\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"第\s*(?P<season>\d{1,2})\s*[季期]",
    ):
        season_only = re.search(pattern, text, flags=re.IGNORECASE)
        if season_only:
            season_only_season = int(season_only.group("season"))
            season_only_span = season_only.span()
            break
    explicit_bracketed_episode_range = re.search(
        r"\[\s*E\s*(?P<episode>\d{1,3})(?:v\d+)?\s*[-_]\s*\d{1,3}\s*\]",
        text,
        flags=re.IGNORECASE,
    )
    if explicit_bracketed_episode_range:
        season = season_only_season if season_only_season is not None else DEFAULT_SEASON
        return season, int(explicit_bracketed_episode_range.group("episode"))
    episode_of_total_text = text
    if season_only_span is not None:
        _, end = season_only_span
        episode_of_total_text = _remove_season_subdivision_metadata(text[end:])
        episode_of_total_text = re.sub(
            r"\[[^\]]*\b(?:parts?|cours?)\.?\s*\d{1,3}\b(?:\s+of\s+\d{1,3}\b)?[^\]]*\]",
            " ",
            episode_of_total_text,
            flags=re.IGNORECASE,
        )
    else:
        episode_of_total_text = _remove_explicit_subdivision_of_total_metadata(episode_of_total_text)
    episode_of_total = re.search(
        r"(?:^|[\s_\-\[\(]|(?<!\d)\.)(?P<episode>\d{1,3})(?:v\d+)?\s+of\s+\d{1,3}(?=$|[\s_\-\]\)]|\.(?!\d))",
        episode_of_total_text,
        flags=re.IGNORECASE,
    )
    if episode_of_total:
        season = season_only_season if season_only_season is not None else DEFAULT_SEASON
        return season, int(episode_of_total.group("episode"))
    episode_range_text = text
    if season_only_span is not None:
        _, end = season_only_span
        episode_range_text = text[end:]
        episode_range_text = re.sub(
            r"\[[^\]]*\b(?:parts?|cours?)\.?\s*\d{1,3}\b[^\]]*\]",
            " ",
            episode_range_text,
            flags=re.IGNORECASE,
        )
        episode_range_text = re.sub(
            r"\[[^\]]*(?:\b\d{1,3}\s*(?:discs?|vol(?:ume)?s?)\b|\b(?:discs?|vol(?:ume)?s?)\.?\s*\d{1,3}\b)[^\]]*\]",
            " ",
            episode_range_text,
            flags=re.IGNORECASE,
        )
    episode_range_text = _remove_season_subdivision_metadata(episode_range_text)
    episode_range_text = _remove_unbracketed_disc_volume_metadata(episode_range_text, preserve_short_title_tokens=False)
    episode_range = re.search(
        r"(?:^|[\s_\-\[\(]|(?<!\d)\.)(?P<episode>\d{1,3})(?:v\d+)?\s*(?P<separator>[-_])\s*(?P<end>\d{1,3})(?=$|[\s_\-\]\)]|\.(?!\d))",
        episode_range_text,
        flags=re.IGNORECASE,
    )
    if episode_range:
        if season_only_season is not None:
            return season_only_season, None
        if not re.search(r"01\s*[-_]\s*02", episode_range.group(0)):
            return DEFAULT_SEASON, None
        return DEFAULT_SEASON, int(episode_range.group("episode"))
    bracketed_episodes = list(
        re.finditer(
            r"\[(?:E\s*)?(?:第\s*)?(?P<episode>\d{1,3})(?:v\d+)?(?:\s*[話话集])?(?:\s+(?:(?:480|720|1080|2160)p|4k)\b[^\]]*)?\]",
            text,
            flags=re.IGNORECASE,
        )
    )
    if bracketed_episodes:
        bracketed_episode = bracketed_episodes[-1]
        season = season_only_season if season_only_season is not None else DEFAULT_SEASON
        return season, int(bracketed_episode.group("episode"))
    candidate_text = text
    if season_only_span is not None:
        _, end = season_only_span
        candidate_text = text[end:]
        candidate_text = re.sub(
            r"\[[^\]]*(?:\b\d{1,3}\s*(?:discs?|vol(?:ume)?s?)\b|\b(?:discs?|vol(?:ume)?s?)\.?\s*\d{1,3}\b)[^\]]*\]",
            " ",
            candidate_text,
            flags=re.IGNORECASE,
        )
        candidate_text = re.sub(
            r"\[[^\]]*\b(?:parts?|cours?)\.?\s*\d{1,3}\b[^\]]*\]",
            " ",
            candidate_text,
            flags=re.IGNORECASE,
        )
        candidate_text = _remove_season_subdivision_metadata(candidate_text)
        candidate_text = _remove_unbracketed_disc_volume_metadata(candidate_text, preserve_short_title_tokens=False)
    candidate_text = re.sub(
        r"\[[^\]]*\b(?:(?:480|720|1080|2160)p|4k)\b[^\]]*\]",
        " ",
        candidate_text,
        flags=re.IGNORECASE,
    )
    candidate_text = re.sub(
        r"\[[^\]]*\b(?:aac|flac|opus|dts|ac3|eac3|avc|hevc|h264|h265|x264|x265|hi10p|bit)\b[^\]]*\]",
        " ",
        candidate_text,
        flags=re.IGNORECASE,
    )
    if season_only_span is None:
        candidate_text = _remove_explicit_subdivision_of_total_metadata(candidate_text)
        candidate_text = _remove_unbracketed_disc_volume_metadata(candidate_text)
    candidates = list(
        re.finditer(
            r"(?:^|[\s_\-\[\(]|(?<!\d)\.)(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
            candidate_text,
            flags=re.IGNORECASE,
        )
    )
    if candidates:
        season = season_only_season if season_only_season is not None else DEFAULT_SEASON
        return season, int(candidates[-1].group("episode"))
    if season_only_season is not None:
        return season_only_season, None
    return DEFAULT_SEASON, None


def _parse_release_group(text: str) -> str | None:
    match = re.search(r"^\s*\[(?P<group>[^\]]+)\]", text)
    return match.group("group") if match else None


def _parse_quality(text: str) -> str | None:
    match = re.search(r"\b(?P<quality>(?:480|720|1080|2160)p|4k|\d{3,4}x\d{3,4})\b", text, flags=re.IGNORECASE)
    return match.group("quality") if match else None


def _season_context_episode_range(text: str) -> int | None:
    range_suffix = r"[\[\(]?\s*\d{1,3}(?:v\d+)?\s*[-_]\s*\d{1,3}\s*[\]\)]?(?=$|[\s_\-\]\)]|\.(?!\d))"
    for pattern in (
        rf"\bS(?P<season>\d{{1,2}})\b[\s_.-]+{range_suffix}",
        rf"\bSeason\s*(?P<season>\d{{1,2}})\b[\s_.-]+{range_suffix}",
        rf"\b(?P<season>\d{{1,2}})(?:st|nd|rd|th)\s+Season\b[\s_.-]+{range_suffix}",
        rf"第\s*(?P<season>\d{{1,2}})\s*[季期][\s_.-]*(?:第\s*)?{range_suffix}",
    ):
        season_range = re.search(pattern, text, flags=re.IGNORECASE)
        if season_range:
            return int(season_range.group("season"))
    return None


def _season_context_e_prefixed_episode_range(text: str) -> int | None:
    range_suffix = r"E\s*\d{1,3}(?:v\d+)?\s*[-_]\s*E\s*\d{1,3}(?=$|[\s_\-\]\)]|\.(?!\d))"
    for pattern in (
        rf"\bS(?P<season>\d{{1,2}})\b[\s_.-]+{range_suffix}",
        rf"\bSeason\s*(?P<season>\d{{1,2}})\b[\s_.-]+{range_suffix}",
        rf"\b(?P<season>\d{{1,2}})(?:st|nd|rd|th)\s+Season\b[\s_.-]+{range_suffix}",
        rf"第\s*(?P<season>\d{{1,2}})\s*[季期][\s_.-]*{range_suffix}",
    ):
        season_range = re.search(pattern, text, flags=re.IGNORECASE)
        if season_range:
            return int(season_range.group("season"))
    return None


def _series_title(title: str, stem: str, release_group: str, quality: str, episode: int | None) -> str:
    value = title or stem
    bracket_series_title = _bracket_series_title(value, release_group, episode)
    had_season_context = _has_season_context(value)
    had_bracketed_episode_marker = _has_bracketed_episode_marker(value, episode)
    leading_group_match = re.match(r"^\s*\[(?P<group>[^\]]+)\]", value)
    had_leading_release_group_marker = bool(
        leading_group_match and leading_group_match.group("group").casefold() == release_group.casefold()
    )
    value = _remove_delimited_episode_title_suffix(value, episode)
    value = re.sub(r"^\s*\[[^\]]+\]\s*", "", value)
    value = re.sub(r"\[[^\]]*\]", " ", value)
    if had_season_context:
        value = _remove_season_subdivision_metadata(_remove_subdivision_after_season_marker(value))
    else:
        value = _remove_no_season_subdivision_metadata(value)
    value = _remove_delimited_episode_title_suffix(value, episode)
    value = re.sub(r"\bS\d{1,2}\b[\s_.-]+E\s*\d{1,3}(?:v\d+)?\s*[-_]\s*E\s*\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bSeason\s*\d{1,2}\b[\s_.-]+E\s*\d{1,3}(?:v\d+)?\s*[-_]\s*E\s*\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\s+Season\b[\s_.-]+E\s*\d{1,3}(?:v\d+)?\s*[-_]\s*E\s*\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"第\s*\d{1,2}\s*[季期][\s_.-]*E\s*\d{1,3}(?:v\d+)?\s*[-_]\s*E\s*\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bS\d{1,2}\b[\s_.-]+E\s*\d{1,3}(?:v\d+)?(?:\s*[-_]\s*\d{1,3})?\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bS\d{1,2}\s*E\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bSeason\s*\d{1,2}\b[\s_.-]+(?:E\s*)?\d{1,3}(?:v\d+)?\s+of\s+\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\s+Season\b[\s_.-]+(?:E\s*)?\d{1,3}(?:v\d+)?\s+of\s+\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"第\s*\d{1,2}\s*[季期][\s_.-]*(?:E\s*)?(?:第\s*)?\d{1,3}(?:v\d+)?\s+of\s+\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bSeason\s*\d{1,2}\b[\s_.-]+(?:E\s*)?\d{1,3}(?:v\d+)?\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\s+Season\b[\s_.-]+(?:E\s*)?\d{1,3}(?:v\d+)?\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"第\s*\d{1,2}\s*[季期][\s_.-]*(?:E\s*)?(?:第\s*)?\d{1,3}(?:v\d+)?\s*[話话集]?", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bS\d{1,2}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bSeason\s*\d{1,2}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\s+Season\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"第\s*\d{1,2}\s*[季期]", " ", value)
    if had_season_context:
        value = _remove_unbracketed_disc_volume_metadata(value)
    else:
        value = _remove_bd_disc_volume_metadata(value)
    if quality:
        value = _remove_title_token(value, quality)
    value = _remove_delimited_episode_title_suffix(value, episode)
    if not had_bracketed_episode_marker:
        value = _remove_trailing_episode_token(value)
    if release_group and not had_leading_release_group_marker:
        if len(release_group.strip()) > 1:
            value = _remove_leading_title_token(value, release_group)
            value = _remove_delimited_title_token(value, release_group)
    series_title = re.sub(r"[\s_.-]+", " ", value).strip()
    return series_title or bracket_series_title


def _bracket_series_title(value: str, release_group: str, episode: int | None) -> str:
    stripped = value.strip()
    matches = list(re.finditer(r"\[([^\]]+)\]", stripped))
    if len(matches) < 2 or matches[0].start() != 0 or stripped[matches[0].end() : matches[1].start()].strip():
        return ""
    first_bracket_fallback = ""
    for index, match in enumerate(matches):
        content = match.group(1).strip()
        if not content:
            continue
        if index == 0 and release_group and content.casefold() == release_group.casefold():
            if not _is_spec_bracket(content, episode):
                first_bracket_fallback = content
            continue
        if _is_spec_bracket(content, episode):
            continue
        return content
    return first_bracket_fallback


def _is_spec_bracket(content: str, episode: int | None) -> bool:
    normalized = content.strip().casefold()
    if not normalized:
        return True
    if episode is not None and re.fullmatch(rf"(?:e\s*)?0*{episode}(?:v\d+)?(?:\s*[話话集])?", normalized, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"(?:e\s*)?(?:第\s*)?0\d{1,2}(?:v\d+)?(?:\s*[話话集])?", normalized, flags=re.IGNORECASE):
        return True
    return bool(
        re.search(r"\b(?:(?:480|720|1080|2160)p|4k|\d{3,4}x\d{3,4})\b", normalized, flags=re.IGNORECASE)
        or re.search(r"\b(?:aac|flac|opus|dts|ac3|eac3|avc|hevc|h264|h265|x264|x265|hi10p|mp4|mkv)\b", normalized, flags=re.IGNORECASE)
        or normalized in {"chs", "cht", "gb", "big5", "sc", "tc", "简", "繁", "简繁", "字幕", "sub", "subs"}
    )


def _has_bracketed_episode_marker(value: str, episode: int | None) -> bool:
    if episode is None:
        return False
    return bool(
        re.search(
            rf"\[(?:E\s*)?(?:第\s*)?0*{episode}(?:v\d+)?(?:\s*[話话集])?(?:\s+(?:(?:480|720|1080|2160)p|4k)\b[^\]]*)?\]",
            value,
            flags=re.IGNORECASE,
        )
    )


def _remove_delimited_episode_title_suffix(value: str, episode: int | None) -> str:
    if episode is None:
        return value
    episode_pattern = rf"0*{episode}(?:v\d+)?"
    delimiter = r"[\s_.-]*[-_.][\s_.-]*"
    episode_marker_pattern = (
        rf"(?:\bS\d{{1,2}}\s*[-_.]?\s*E\s*{episode_pattern}\b"
        rf"|\bSeason\s*\d{{1,2}}\b[\s_.-]+E\s*{episode_pattern}\b"
        rf"|\b\d{{1,2}}(?:st|nd|rd|th)\s+Season\b[\s_.-]+E\s*{episode_pattern}\b"
        rf"|第\s*\d{{1,2}}\s*[季期][\s_.-]*(?:E\s*)?(?:第\s*)?{episode_pattern}\s*[話话集]?"
        rf"|\[(?:E\s*)?(?:第\s*)?{episode_pattern}(?:\s*[話话集])?\])"
    )
    value = re.sub(
        rf"(?P<prefix>.*?)(?:^|[\s_.-]+){episode_marker_pattern}{delimiter}\S.*$",
        lambda match: match.group("prefix"),
        value,
        flags=re.IGNORECASE,
    )
    def remove_delimited_suffix(match: re.Match[str]) -> str:
        if re.search(r"(?:^|[\s_.-])(?:discs?|vol(?:ume)?s?)\.?\s*$", match.group("prefix"), flags=re.IGNORECASE):
            return match.group(0)
        return match.group("prefix")

    return re.sub(
        rf"(?P<prefix>.*?){delimiter}{episode_pattern}{delimiter}\S.*$",
        remove_delimited_suffix,
        value,
        flags=re.IGNORECASE,
    )


def _remove_trailing_episode_token(value: str) -> str:
    value = re.sub(
        r"(?P<prefix>^|[\s_.-])\d{1,3}(?:v\d+)?\s+of\s+\d{1,3}[\s_.-]*$",
        lambda match: match.group("prefix"),
        value,
        flags=re.IGNORECASE,
    )
    def remove_trailing_range(match: re.Match[str]) -> str:
        if re.search(r"(?:^|[\s_.-])(?:parts?|cours?|discs?|vol(?:ume)?s?)\.?\s*$", value[: match.start()], flags=re.IGNORECASE):
            return match.group(0)
        if int(match.group("start")) <= int(match.group("end")):
            return match.group("prefix")
        return match.group(0)

    value = re.sub(
        r"(?P<prefix>^|[\s_.-])(?P<start>\d{1,3})(?:v\d+)?\s*[-_]\s*(?P<end>\d{1,3})[\s_.-]*$",
        remove_trailing_range,
        value,
        flags=re.IGNORECASE,
    )
    def remove_trailing_number(match: re.Match[str]) -> str:
        if re.search(r"(?:^|[\s_.-])(?:parts?|cours?|discs?|vol(?:ume)?s?)\.?\s*$", value[: match.start()], flags=re.IGNORECASE):
            return match.group(0)
        return match.group("prefix")

    return re.sub(r"(?P<prefix>^|[\s_.-])\d{1,3}(?:v\d+)?[\s_.-]*$", remove_trailing_number, value)


def _remove_unbracketed_disc_volume_metadata(value: str, *, preserve_short_title_tokens: bool = True) -> str:
    def remove_clear_disc_volume(match: re.Match[str]) -> str:
        if preserve_short_title_tokens and not match.group("bd"):
            title_words_before_marker = re.findall(r"[A-Za-z0-9]+", value[: match.start()])
            if len(title_words_before_marker) < 2:
                return match.group(0)
        return " "

    return re.sub(
        r"(?:^|[\s_.-])(?P<bd>BD[\s_.-]+)?(?:\d{1,3}\s*(?:discs?|vol(?:ume)?s?)\b|(?:discs?|vol(?:ume)?s?)\.?\s*\d{1,3}\b)(?=\s*$|[\s_.-]+(?:\d{1,3}\b|E\s*\d{1,3}\b|\[[^\]]*\]))",
        remove_clear_disc_volume,
        value,
        flags=re.IGNORECASE,
    )


def _remove_bd_disc_volume_metadata(value: str) -> str:
    return re.sub(
        r"(?:^|[\s_.-])BD[\s_.-]+(?:\d{1,3}\s*(?:discs?|vol(?:ume)?s?)\b|(?:discs?|vol(?:ume)?s?)\.?\s*\d{1,3}\b)(?=\s*$|[\s_.-]+(?:\d{1,3}\b|E\s*\d{1,3}\b|\[[^\]]*\]))",
        " ",
        value,
        flags=re.IGNORECASE,
    )


def _remove_explicit_subdivision_of_total_metadata(value: str) -> str:
    return re.sub(
        r"(?:^|[\s_.-])(?:parts?|cours?)\.?\s*\d{1,3}\b\s+of\s+\d{1,3}\b(?=\s*$|\s*(?:[\[\(]|[-_.]\s*)?(?:E\s*)?\d{1,3}\b)",
        " ",
        value,
        flags=re.IGNORECASE,
    )


def _remove_no_season_subdivision_metadata(value: str) -> str:
    value = _remove_explicit_subdivision_of_total_metadata(value)
    return re.sub(
        r"(?:^|[\s_.-])(?:parts?|cours?)\.?\s*\d{1,3}\b\s+of\s+\d{1,3}\b(?=\s*(?:[\[\(]|[-_.]\s*)?(?:E\s*)?\d{1,3}\b)",
        " ",
        value,
        flags=re.IGNORECASE,
    )


def _remove_season_subdivision_metadata(value: str) -> str:
    metadata_context = r"(?=\s*$|\s*\[[^\]]*\]|\s*(?:[-_.]\s*)?(?:E\s*)?\d{1,3}\b)"
    return re.sub(
        rf"(?:^|[\s_.-])(?:parts?|cours?)\.?\s*\d{{1,3}}\b(?:\s+of\s+\d{{1,3}}\b)?{metadata_context}",
        " ",
        value,
        flags=re.IGNORECASE,
    )


def _remove_subdivision_after_season_marker(value: str) -> str:
    metadata_context = r"(?=\s*$|\s*\[[^\]]*\]|\s*(?:[-_.]\s*)?(?:E\s*)?\d{1,3}\b)"
    metadata_suffix = rf"(?:parts?|cours?)\.?\s*\d{{1,3}}\b(?:\s+of\s+\d{{1,3}}\b)?{metadata_context}"
    for pattern in (
        rf"(?P<season>\bS\d{{1,2}}\b)[\s_.-]+{metadata_suffix}",
        rf"(?P<season>\bSeason\s*\d{{1,2}}\b)[\s_.-]+{metadata_suffix}",
        rf"(?P<season>\b\d{{1,2}}(?:st|nd|rd|th)\s+Season\b)[\s_.-]+{metadata_suffix}",
        rf"(?P<season>第\s*\d{{1,2}}\s*[季期])[\s_.-]*{metadata_suffix}",
    ):
        value = re.sub(pattern, lambda match: match.group("season"), value, flags=re.IGNORECASE)
    return value


def _remove_leading_title_token(value: str, token: str) -> str:
    token_pattern = re.escape(token)
    return re.sub(rf"^\s*{token_pattern}(?=$|[\s_.-])", " ", value, flags=re.IGNORECASE)


def _remove_delimited_title_token(value: str, token: str) -> str:
    token_pattern = re.escape(token)
    return re.sub(
        rf"(?P<left>^|[\s_.-]*[-_.][\s_.-]*){token_pattern}(?=$|[\s_.-]*[-_.])",
        lambda match: match.group("left"),
        value,
        flags=re.IGNORECASE,
    )


def _remove_title_token(value: str, token: str) -> str:
    return re.sub(rf"(?<![^\W_]){re.escape(token)}(?![^\W_])", " ", value, flags=re.IGNORECASE)


def _lookup_title(title: str, stem: str, series_title: str) -> str:
    for value in (title, stem):
        value = value.strip()
        if value and _has_season_context(value):
            return value
    return _primary_title_alias(series_title) or series_title.strip() or title.strip() or stem.strip()


def _primary_title_alias(value: str) -> str:
    """Return the cleanest non-season title alias for external metadata lookup."""

    if "://" not in value and (separator := re.search(r"(?<!:)/{2,}", value)):
        left_alias = value[: separator.start()].strip()
        return left_alias or _first_non_empty_slash_alias(value[separator.end() :])

    for match in re.finditer(r"/", value):
        left = value[: match.start()]
        right = value[match.end() :]
        left_alias = left.strip()
        right_alias = right.strip()
        left_spaced = bool(left) and left[-1].isspace()
        right_spaced = bool(right) and right[0].isspace()
        path_like = _has_path_like_continuation(value[match.start() :])
        if path_like:
            return value.strip()
        if left_spaced or right_spaced:
            return left_alias or _first_non_empty_slash_alias(right)
        if not left_alias or not right_alias:
            return left_alias or right_alias
        left_script = _nearest_title_script(value, match.start() - 1, -1)
        right_script = _nearest_title_script(value, match.end(), 1)
        if {left_script, right_script} == {"latin", "cjk"}:
            return left_alias
    return value.strip()


def _first_non_empty_slash_alias(value: str) -> str:
    for alias in (part.strip() for part in value.split("/")):
        if alias:
            return alias
    return ""


def _has_path_like_continuation(value: str) -> bool:
    stripped = value.lstrip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return False
    token = stripped.split(maxsplit=1)[0][1:]
    first_segment, _, _ = token.partition("/")
    return bool(first_segment) and first_segment.casefold() in PATH_LIKE_FIRST_SEGMENTS


def _nearest_title_script(value: str, index: int, step: int) -> str:
    while 0 <= index < len(value):
        character = value[index]
        if character == "/":
            return ""
        if _is_latin(character):
            return "latin"
        if _is_cjk(character):
            return "cjk"
        index += step
    return ""


def _is_latin(value: str) -> bool:
    return ("A" <= value <= "Z") or ("a" <= value <= "z")


def _is_cjk(value: str) -> bool:
    codepoint = ord(value)
    return (
        0x3400 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _has_season_context(value: str) -> bool:
    return bool(
        re.search(r"\bS\d{1,2}(?:\s*E\d{1,3})?\b", value, flags=re.IGNORECASE)
        or re.search(r"\bSeason\s*\d{1,2}\b", value, flags=re.IGNORECASE)
        or re.search(r"\b\d{1,2}(?:st|nd|rd|th)\s+Season\b", value, flags=re.IGNORECASE)
        or re.search(r"第\s*\d{1,2}\s*[季期]", value)
    )


def _metadata_text(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _video_destination(library_root: Path, info: _EpisodeInfo, suffix: str) -> Path:
    if info.episode is None:
        if info.flat_library:
            return library_root / info.library_title / f"{info.title}{suffix.casefold()}"
        return library_root / "_Unsorted" / info.title / f"{info.title}{suffix.casefold()}"
    return _season_directory(library_root, info) / f"{info.title} - S{info.season:02d}E{info.episode:02d} - {info.release_group} [{info.quality}]{suffix.casefold()}"


def _subtitle_destination(library_root: Path, info: _EpisodeInfo, video: Path, subtitle: Path) -> Path:
    video_destination = _video_destination(library_root, info, video.suffix)
    return video_destination.with_suffix(subtitle.suffix.casefold())


def _season_directory(library_root: Path, info: _EpisodeInfo) -> Path:
    if info.flat_library:
        return library_root / info.library_title
    return library_root / info.library_title / f"Season {info.season:02d}"


def _plan_action(source: Path, destination: Path, library_root: Path, mode: OrganizerMode, media_type: str, info: _EpisodeInfo) -> OrganizerAction:
    if not info.flat_library and info.library_title == "Unknown Series":
        return OrganizerAction(source, None, "unsorted", media_type, "Series title could not be parsed", info.episode, info.season)
    if not _is_relative_to(destination.resolve(strict=False), library_root):
        return OrganizerAction(source, None, "conflict", media_type, "Destination escaped library root", info.episode, info.season)
    if destination.exists():
        return OrganizerAction(source, destination, "conflict", media_type, "Destination already exists", info.episode, info.season)
    if info.episode is None:
        return OrganizerAction(source, destination, "unsorted", media_type, "Episode could not be parsed", None, info.season)
    status = "planned" if mode is OrganizerMode.DRY_RUN else "planned"
    return OrganizerAction(source, destination, status, media_type, None, info.episode, info.season)


def _apply_action(action: OrganizerAction) -> OrganizerAction:
    if action.destination_path is None:
        return action
    action.destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(action.source_path, action.destination_path)
    return OrganizerAction(
        source_path=action.source_path,
        destination_path=action.destination_path,
        status="applied",
        media_type=action.media_type,
        reason=action.reason,
        episode=action.episode,
        season=action.season,
    )


def _matching_video(subtitle: Path, videos: tuple[Path, ...]) -> Path | None:
    for video in videos:
        if video.stem.casefold() == subtitle.stem.casefold():
            return video
    for video in videos:
        if video.parent.resolve(strict=False) == subtitle.parent.resolve(strict=False):
            return video
    return None


def _sanitize_segment(value: str) -> str:
    sanitized = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", value)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    return sanitized


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _events(organizer_input: OrganizerInput, actions: list[OrganizerAction]) -> tuple[NotificationEvent, ...]:
    events: list[NotificationEvent] = []
    for action in actions:
        if action.status not in {"conflict", "unsorted"}:
            continue
        events.append(
            NotificationEvent(
                event_type=f"organizer_{action.status}",
                title=organizer_input.title,
                message=action.reason or action.status,
                job_id=organizer_input.job_id,
                severity="warning",
                metadata={
                    "source_path": str(action.source_path),
                    "destination_path": str(action.destination_path) if action.destination_path else None,
                    "media_type": action.media_type,
                },
            )
        )
    return tuple(events)
