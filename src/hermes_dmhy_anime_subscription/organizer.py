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
    release_group = _metadata_text(metadata, "release_group") or _parse_release_group(text) or "Unknown"
    quality = _metadata_text(metadata, "quality") or _parse_quality(text) or "Unknown"
    series_title = _metadata_text(metadata, "series_title") or _series_title(title, path.stem, release_group, quality)
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
    return replace(info, library_title=library_title, flat_library=True)


def _parse_episode(text: str) -> tuple[int, int | None]:
    season_episode = re.search(r"\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})\b", text, flags=re.IGNORECASE)
    if season_episode:
        return int(season_episode.group("season")), int(season_episode.group("episode"))
    season_then_episode = re.search(
        r"\bS(?P<season>\d{1,2})\b[\s_.-]+(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
        text,
        flags=re.IGNORECASE,
    )
    if season_then_episode:
        return int(season_then_episode.group("season")), int(season_then_episode.group("episode"))
    for pattern in (
        r"\bSeason\s*(?P<season>\d{1,2})\b[\s_.-]+(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
        r"\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+Season\b[\s_.-]+(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
        r"第\s*(?P<season>\d{1,2})\s*[季期][\s_.-]*(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
    ):
        season_word_episode = re.search(pattern, text, flags=re.IGNORECASE)
        if season_word_episode:
            return int(season_word_episode.group("season")), int(season_word_episode.group("episode"))
    episode_of_total = re.search(
        r"(?:^|[\s_\-\[\(]|(?<!\d)\.)(?P<episode>\d{1,3})(?:v\d+)?\s+of\s+\d{1,3}(?=$|[\s_\-\]\)]|\.(?!\d))",
        text,
        flags=re.IGNORECASE,
    )
    if episode_of_total:
        return DEFAULT_SEASON, int(episode_of_total.group("episode"))
    episode_range = re.search(
        r"(?:^|[\s_\-\[\(]|(?<!\d)\.)(?P<episode>\d{1,3})(?:v\d+)?[-_]\d{1,3}(?=$|[\s_\-\]\)]|\.(?!\d))",
        text,
        flags=re.IGNORECASE,
    )
    if episode_range:
        return DEFAULT_SEASON, int(episode_range.group("episode"))
    bracketed_episode = re.search(
        r"\[(?P<episode>\d{1,3})(?:v\d+)?(?:\s+(?:(?:480|720|1080|2160)p|4k)\b[^\]]*)?\]",
        text,
        flags=re.IGNORECASE,
    )
    if bracketed_episode:
        return DEFAULT_SEASON, int(bracketed_episode.group("episode"))
    for pattern in (
        r"\bS(?P<season>\d{1,2})\b",
        r"\bSeason\s*(?P<season>\d{1,2})\b",
        r"\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"第\s*(?P<season>\d{1,2})\s*[季期]",
    ):
        season_only = re.search(pattern, text, flags=re.IGNORECASE)
        if season_only:
            return int(season_only.group("season")), None
    candidate_text = re.sub(
        r"\[[^\]]*\b(?:(?:480|720|1080|2160)p|4k)\b[^\]]*\]",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    candidate_text = re.sub(
        r"\[[^\]]*\b(?:aac|flac|opus|dts|ac3|eac3|avc|hevc|h264|h265|x264|x265|hi10p|bit)\b[^\]]*\]",
        " ",
        candidate_text,
        flags=re.IGNORECASE,
    )
    candidates = list(
        re.finditer(
            r"(?:^|[\s_\-\[\(]|(?<!\d)\.)(?P<episode>\d{1,3})(?:v\d+)?(?=$|[\s_\-\]\)]|\.(?!\d)|\.(?=(?:(?:480|720|1080|2160)p|4k)\b))",
            candidate_text,
            flags=re.IGNORECASE,
        )
    )
    if candidates:
        return DEFAULT_SEASON, int(candidates[-1].group("episode"))
    return DEFAULT_SEASON, None


def _parse_release_group(text: str) -> str | None:
    match = re.search(r"^\s*\[(?P<group>[^\]]+)\]", text)
    return match.group("group") if match else None


def _parse_quality(text: str) -> str | None:
    match = re.search(r"\b(?P<quality>(?:480|720|1080|2160)p|4k)\b", text, flags=re.IGNORECASE)
    return match.group("quality") if match else None


def _series_title(title: str, stem: str, release_group: str, quality: str) -> str:
    value = title or stem
    leading_group_match = re.match(r"^\s*\[(?P<group>[^\]]+)\]", value)
    had_leading_release_group_marker = bool(
        leading_group_match and leading_group_match.group("group").casefold() == release_group.casefold()
    )
    value = re.sub(r"^\s*\[[^\]]+\]\s*", "", value)
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"\bS\d{1,2}\s*E\d{1,3}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bS\d{1,2}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bSeason\s*\d{1,2}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\s+Season\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"第\s*\d{1,2}\s*[季期]", " ", value)
    if quality:
        value = _remove_title_token(value, quality)
    value = _remove_trailing_episode_token(value)
    if release_group and not had_leading_release_group_marker:
        if len(release_group.strip()) > 1:
            value = _remove_leading_title_token(value, release_group)
            value = _remove_delimited_title_token(value, release_group)
    return re.sub(r"[\s_.-]+", " ", value).strip()


def _remove_trailing_episode_token(value: str) -> str:
    value = re.sub(
        r"(?P<prefix>^|[\s_.-])\d{1,3}(?:v\d+)?\s+of\s+\d{1,3}[\s_.-]*$",
        lambda match: match.group("prefix"),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"(?P<prefix>^|[\s_.-])\d{1,3}(?:v\d+)?[-_]\d{1,3}[\s_.-]*$",
        lambda match: match.group("prefix"),
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"(?P<prefix>^|[\s_.-])\d{1,3}(?:v\d+)?[\s_.-]*$", lambda match: match.group("prefix"), value)


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
