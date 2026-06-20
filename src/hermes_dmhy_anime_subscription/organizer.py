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
class EpisodeParserResult:
    series_title: str | None = None
    season: int | None = None
    episode: int | None = None
    release_group: str | None = None
    quality: str | None = None


EpisodeParser = Callable[[str], EpisodeParserResult | None]


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


def organize_media(
    organizer_input: OrganizerInput,
    config: OrganizerConfig,
    *,
    bangumi_lookup: BangumiLookup | None = None,
    episode_parser: EpisodeParser | None = None,
) -> OrganizerResult:
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
            episode_parser=episode_parser,
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
            info = _episode_info(video, organizer_input.title, organizer_input.metadata, episode_parser=episode_parser)
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
    path: Path,
    title: str,
    metadata: dict[str, object],
    *,
    prefer_stem_episode: bool = False,
    episode_parser: EpisodeParser | None = None,
) -> _EpisodeInfo:
    title_parse = _parse_filename(title) if title else _ParsedFilename.unknown()
    title_parse = _with_episode_parser(title, title_parse, episode_parser)
    stem_parse = _parse_filename(path.stem)
    if prefer_stem_episode:
        stem_episode = _single_episode_token(path.stem)
        if stem_episode is not None:
            stem_parse = replace(stem_parse, series_title="", lookup_title="", episode=stem_episode, structured_title=False)
    if not _has_title_and_episode(title_parse) or (prefer_stem_episode and stem_parse.episode is None):
        stem_parse = _with_episode_parser(path.stem, stem_parse, episode_parser)
    title_has_structured_title = bool(title_parse.series_title and title_parse.structured_title)
    stem_has_structured_title = bool(stem_parse.series_title and stem_parse.structured_title)
    if prefer_stem_episode and stem_parse.episode is not None and title_has_structured_title:
        selected_episode = stem_parse.episode
        selected_season = title_parse.season if title_parse.season != DEFAULT_SEASON else stem_parse.season
    elif title_parse.episode is not None and title_has_structured_title:
        selected_episode = title_parse.episode
        selected_season = title_parse.season
    elif stem_parse.episode is not None and (title_has_structured_title or stem_has_structured_title):
        selected_episode = stem_parse.episode
        selected_season = title_parse.season if title_parse.season != DEFAULT_SEASON else stem_parse.season
    else:
        selected_episode = None
        selected_season = title_parse.season if title_parse.season != DEFAULT_SEASON else stem_parse.season

    release_group = _metadata_text(metadata, "release_group") or title_parse.release_group or stem_parse.release_group or "Unknown"
    quality = _metadata_text(metadata, "quality") or title_parse.quality or stem_parse.quality or "Unknown"
    series_title = (
        _metadata_text(metadata, "series_title")
        or (title_parse.series_title if title_has_structured_title or selected_episode is None else "")
        or (stem_parse.series_title if stem_has_structured_title else "")
        or title_parse.series_title
        or "Unknown Series"
    )
    lookup_title = (
        (title_parse.lookup_title if title_has_structured_title or selected_episode is None else "")
        or (stem_parse.lookup_title if stem_has_structured_title else "")
        or title_parse.lookup_title
        or series_title
    )
    return _EpisodeInfo(
        title=_sanitize_segment(series_title) or "Unknown Series",
        lookup_title=lookup_title,
        library_title=_sanitize_segment(series_title) or "Unknown Series",
        flat_library=False,
        season=selected_season,
        episode=selected_episode,
        release_group=_sanitize_segment(release_group) or "Unknown",
        quality=_sanitize_segment(quality) or "Unknown",
    )


@dataclass(frozen=True, slots=True)
class _ParsedFilename:
    series_title: str
    lookup_title: str
    season: int
    episode: int | None
    release_group: str | None
    quality: str | None
    structured_title: bool

    @classmethod
    def unknown(cls) -> "_ParsedFilename":
        return cls("", "", DEFAULT_SEASON, None, None, None, False)


def _parse_filename(text: str) -> _ParsedFilename:
    value = text.strip()
    if not value:
        return _ParsedFilename.unknown()
    quality = _parse_quality(value)
    release_group, body = _split_release_group(value)
    bracket_parse = _parse_consecutive_brackets(body, release_group, quality)
    if bracket_parse is not None:
        return bracket_parse
    season = _parse_season_only(body) or DEFAULT_SEASON
    if _needs_fallback(body):
        fallback_title = _remove_season_markers(body)
        return _ParsedFilename(_clean_series_title(fallback_title), _lookup_title_from_body(fallback_title), season, None, release_group, quality, False)
    for parser in (_parse_sxxexx, _parse_season_episode, _parse_cjk_season_episode, _parse_delimited_episode):
        parsed = parser(body, season)
        if parsed is not None:
            parsed_title, parsed_season, episode = parsed
            return _ParsedFilename(_clean_series_title(parsed_title), _lookup_title_from_body(parsed_title), parsed_season, episode, release_group, quality, True)
    title = _remove_season_markers(body)
    return _ParsedFilename(_clean_series_title(title), _lookup_title_from_body(title), season, None, release_group, quality, True)


def _with_episode_parser(text: str, parsed: _ParsedFilename, episode_parser: EpisodeParser | None) -> _ParsedFilename:
    if episode_parser is None or not text.strip() or _has_title_and_episode(parsed):
        return parsed
    try:
        fallback = episode_parser(text)
    except Exception:
        return parsed
    if fallback is None:
        return parsed
    has_parser_title = bool(fallback.series_title and fallback.series_title.strip())
    series_title = _clean_series_title(fallback.series_title) if has_parser_title else parsed.series_title
    lookup_title = _lookup_title_from_body(series_title) if has_parser_title and series_title else parsed.lookup_title
    season = fallback.season if fallback.season and fallback.season > 0 else parsed.season
    episode = fallback.episode if fallback.episode and fallback.episode > 0 and (has_parser_title or parsed.structured_title) else parsed.episode
    release_group = parsed.release_group if fallback.release_group is None else fallback.release_group.strip() or None
    quality = parsed.quality if fallback.quality is None else fallback.quality.strip() or None
    return _ParsedFilename(series_title, lookup_title, season, episode, release_group, quality, parsed.structured_title or has_parser_title)


def _has_title_and_episode(parsed: _ParsedFilename) -> bool:
    return bool(parsed.series_title and parsed.episode is not None and parsed.structured_title)


def _parse_episode(text: str) -> tuple[int, int | None]:
    parsed = _parse_filename(text)
    return parsed.season, parsed.episode


def _split_release_group(value: str) -> tuple[str | None, str]:
    match = re.match(r"^\s*\[(?P<group>[^\]]+)\]\s*(?P<body>.*)$", value)
    if not match:
        return None, value
    group = match.group("group").strip()
    body = match.group("body").strip()
    known_group = re.search(r"\b(?:Sub|Subs|字幕|字幕组|字幕組|ANi|LoliHouse|DMG|SumiSora|Nekomoe|Lilith)\b", group, flags=re.IGNORECASE)
    if not group:
        return None, value
    if known_group or re.match(r"^\[[^\]]+\]", body):
        return group, body
    if _is_spec_bracket(group, None):
        return None, value
    if body:
        return group, body
    return None, value


def _parse_consecutive_brackets(body: str, release_group: str | None, quality: str | None) -> _ParsedFilename | None:
    stripped = body.strip()
    matches = list(re.finditer(r"\[([^\]]+)\]", stripped))
    if len(matches) < 2 or matches[0].start() != 0:
        return None
    if any(stripped[matches[index].end() : matches[index + 1].start()].strip() for index in range(len(matches) - 1)):
        return None
    title = ""
    episode: int | None = None
    episode_token_count = 0
    for match in matches:
        content = match.group(1).strip()
        possible_episode = _single_episode_token(content)
        if not content or _is_spec_bracket(content, None):
            if title and possible_episode is not None:
                episode_token_count += 1
                if episode_token_count > 1:
                    return _ParsedFilename.unknown()
                episode = possible_episode
            continue
        if title:
            if possible_episode is not None:
                episode_token_count += 1
                if episode_token_count > 1:
                    return _ParsedFilename.unknown()
                episode = possible_episode
            continue
        title = content
    if not title:
        return _ParsedFilename.unknown()
    if _has_unsupported_season_label(title):
        return _ParsedFilename(
            _clean_series_title(title),
            _lookup_title_from_body(title),
            DEFAULT_SEASON,
            None,
            release_group,
            quality,
            False,
        )
    season = _parse_season_only(title) or DEFAULT_SEASON
    title_without_season = _remove_season_markers(title)
    return _ParsedFilename(_clean_series_title(title_without_season), _lookup_title_from_body(title_without_season), season, episode, release_group, quality, True)


def _parse_sxxexx(body: str, default_season: int) -> tuple[str, int, int] | None:
    match = re.search(r"(?P<title>.*?)\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})\b", body, flags=re.IGNORECASE)
    if not match:
        return None
    title = re.sub(r"[\s\[(]+$", " ", match.group("title"))
    return title, int(match.group("season")), int(match.group("episode"))


def _parse_season_episode(body: str, default_season: int) -> tuple[str, int, int] | None:
    patterns = (
        r"(?P<title>.*?)\bS(?P<season>\d{1,2})\b\s*[-_. ]+\s*(?P<episode>\d{1,3})(?=$|[\s_\-\]\[])",
        r"(?P<title>.*?)\bSeason\s*(?P<season>\d{1,2})\b\s*[-_. ]+\s*(?P<episode>\d{1,3})(?=$|[\s_\-\]\[])",
    )
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            return match.group("title"), int(match.group("season")), int(match.group("episode"))
    return None


def _parse_cjk_season_episode(body: str, default_season: int) -> tuple[str, int, int] | None:
    match = re.search(r"(?P<title>.*?)第\s*(?P<season>\d{1,2})\s*[季期]\s*第?\s*(?P<episode>\d{1,3})\s*[話话集]", body)
    if not match:
        return None
    return match.group("title"), int(match.group("season")), int(match.group("episode"))


def _parse_delimited_episode(body: str, default_season: int) -> tuple[str, int, int] | None:
    without_specs = _strip_spec_brackets(body)
    match = re.search(r"(?P<title>.+?)\s+-\s+(?P<episode>\d{1,3})(?:v\d+)?\s*$", without_specs, flags=re.IGNORECASE)
    if not match:
        return None
    return _remove_season_markers(match.group("title")), default_season, int(match.group("episode"))


def _single_episode_token(value: str) -> int | None:
    match = re.fullmatch(r"(?:E\s*)?(?:第\s*)?(?P<episode>0*\d{1,3})(?:v\d+)?(?:\s*[話话集])?", value.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    episode = int(match.group("episode"))
    return episode if episode > 0 else None


def _parse_season_only(body: str) -> int | None:
    for pattern in (
        r"\bS(?P<season>\d{1,2})\b",
        r"\bSeason\s*(?P<season>\d{1,2})\b",
        r"\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"第\s*(?P<season>\d{1,2})\s*[季期]",
    ):
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            return int(match.group("season"))
    return None


def _has_unsupported_season_label(value: str) -> bool:
    word_ordinal = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|final|last)"
    return bool(
        re.search(rf"\b{word_ordinal}\s+season\b", value, flags=re.IGNORECASE)
        or re.search(rf"\bseason\s+{word_ordinal}\b", value, flags=re.IGNORECASE)
    )


def _needs_fallback(body: str) -> bool:
    stripped = _strip_spec_brackets(body)
    if _has_unsupported_season_label(stripped):
        return True
    range_text = _remove_season_markers(stripped)
    if re.search(r"(?:^|[\s\[\(-])(?:E?\d{1,3})\s*[-_]\s*(?:E?\d{1,3})(?=$|[\s\]\)-])", range_text, flags=re.IGNORECASE):
        return True
    if re.search(r"\b(?:part|cour|disc|discs|vol|volume)\.?\s*\d{1,3}\b", body, flags=re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,3}\s*(?:discs?|vol(?:ume)?s?)\b", body, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[\s\[\]\d_.-]+", body):
        return True
    return False


def _strip_spec_brackets(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        return " " if _is_spec_bracket(content, None) else f" {content} "

    return re.sub(r"\[([^\]]+)\]", replace, value)


def _strip_brackets(value: str) -> str:
    return re.sub(r"\[([^\]]+)\]", r" \1 ", value)


def _remove_season_markers(value: str) -> str:
    value = re.sub(r"\bS\d{1,2}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bSeason\s*\d{1,2}\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\s+Season\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"第\s*\d{1,2}\s*[季期]", " ", value)
    return value


def _clean_series_title(value: str) -> str:
    value = _strip_spec_brackets(value)
    value = re.sub(r"^\s*\[[^\]]+\]\s*", " ", value)
    value = re.sub(r"[\s_.-]+", " ", value)
    return value.strip()


def _lookup_title_from_body(value: str) -> str:
    return _primary_title_alias(_clean_series_title(value))



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
    return replace(info, title=library_title, library_title=library_title, flat_library=info.episode is not None)


def _parse_release_group(text: str) -> str | None:
    match = re.search(r"^\s*\[(?P<group>[^\]]+)\]", text)
    return match.group("group") if match else None


def _parse_quality(text: str) -> str | None:
    match = re.search(r"\b(?P<quality>(?:480|720|1080|2160)p|4k|\d{3,4}x\d{3,4})\b", text, flags=re.IGNORECASE)
    return match.group("quality") if match else None


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
