from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VERSION = 1
# November 19, 2026 at 00:00 in Africa/Nairobi.
GTA_VI_PUBLICATION_CUTOFF = "2026-11-18T21:00:00Z"
NOVEMBER_18_CUTOFF = "2026-11-17T21:00:00Z"
WEIGHTS = {"hook": .25, "payoff": .25, "context": .20, "specificity": .15, "pace": .10, "readability": .05}
THEMES = ("chase", "driving", "stunt", "mission", "party", "radio", "podcast", "theory", "discovery", "comedy", "other")


def now() -> datetime:
    return datetime.now(UTC)


def dated(days_ago: int = 0) -> str:
    return (now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def recent_rejections(ledger: dict) -> dict:
    cutoff = dated(14)
    return {row["id"]: row for row in ledger.get("rejected", []) if row["date"] >= cutoff}


def source_cutoff(days: int) -> str:
    earliest = NOVEMBER_18_CUTOFF if os.getenv("INCLUDE_NOV18") == "true" else GTA_VI_PUBLICATION_CUTOFF
    return max(earliest, dated(days))


def digest(*pieces: object) -> str:
    key = json.dumps(pieces, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def timestamp(seconds: float) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def duration_text(value: str) -> float:
    match = re.fullmatch(r"(?:PT)?(?:(\d+)[Hh])?(?:(\d+)[Mm])?(?:(\d+(?:\.\d+)?)[Ss])?", value)
    if not match:
        return 0
    hours, minutes, seconds = (float(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def clean(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def number(value: object, low: float, high: float) -> float:
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"Expected a number between {low} and {high}")
    return float(value)


@dataclass
class Source:
    key: str
    url: str
    platform: str
    title: str = ""
    creator: str = ""
    duration: float = 0
    views: int = 0
    published: str = ""
    rights: str = "unspecified"
    anchors: list[float] = field(default_factory=list)
    parent: str = ""
    parent_offset: float | None = None
    recording: bool = False
    recorded: str = ""

    @property
    def site(self) -> str:
        return self.platform.split("-", 1)[0]

    @property
    def footage_date(self) -> str:
        return min(self.published, self.recorded) if self.recorded else self.published


def source_url(url: str) -> Source | None:
    """Accept canonical public player URLs from the two discovery platforms."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password:
        return None
    host = parts.hostname
    segments = parts.path.strip("/").split("/")
    if host in {"www.youtube.com", "m.youtube.com", "youtube.com", "youtu.be"}:
        if host == "youtu.be":
            video = segments[0]
        elif segments[0] == "watch":
            video = parse_qs(parts.query).get("v", [""])[0]
        elif len(segments) == 2 and segments[0] in {"live", "shorts"}:
            video = segments[1]
        else:
            return None
        if re.fullmatch(r"[\w-]{11}", video, re.ASCII):
            return Source(f"youtube:{video}", f"https://www.youtube.com/watch?v={video}", "youtube")
    if host in {"www.twitch.tv", "twitch.tv", "clips.twitch.tv"}:
        if len(segments) == 2 and segments[0] == "videos" and segments[1].isascii() and segments[1].isdigit():
            video = segments[1]
            return Source(f"twitch-vod:{video}", f"https://www.twitch.tv/videos/{video}", "twitch-vod")
        if host == "clips.twitch.tv" and len(segments) == 1:
            clip = segments[0]
        elif len(segments) == 3 and segments[1] == "clip":
            clip = segments[2]
        else:
            return None
        if re.fullmatch(r"[\w-]{3,120}", clip, re.ASCII):
            return Source(f"twitch-clip:{clip}", f"https://clips.twitch.tv/{clip}", "twitch-clip")
    return None


@dataclass
class Range:
    source: str
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start

    @property
    def identity(self) -> str:
        return digest(self.source, round(self.start, 1), round(self.end, 1))


def other_game_ranges(ledger: dict) -> list[Range]:
    return [Range(**span) for row in recent_rejections(ledger).values() for span in row.get("other_game_ranges", [])]


@dataclass
class Moment:
    span: Range
    theme: str
    hook: str
    setup: str
    payoff: str
    evidence: str
    score: float
    fingerprint: str = ""
    gta_vi_evidence: str = ""
    alternate_cuts: list[Moment] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return self.span.identity

    def to_dict(self) -> dict:
        return {"id": self.identity, **asdict(self)}

    @classmethod
    def from_dict(cls, row: dict) -> Moment:
        return cls(Range(**row["span"]), row["theme"], row["hook"], row["setup"], row["payoff"], row["evidence"],
                   row["score"], row.get("fingerprint", ""), row.get("gta_vi_evidence", ""),
                   [cls.from_dict(cut) for cut in row.get("alternate_cuts", [])])


@dataclass
class Sequence:
    span: Range
    theme: str
    story: str
    score: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict) -> Sequence:
        return cls(Range(**row["span"]), row["theme"], row["story"], row["score"])


@dataclass
class Cut:
    span: Range
    label: str = ""
    rank: int | None = None
    fingerprint: str = ""


@dataclass
class Edit:
    cuts: list[Cut]
    theme: str
    idea: str
    context: str
    gta_vi_evidence: str = ""

    @property
    def identity(self) -> str:
        return digest([cut.span.identity for cut in self.cuts])

    @property
    def seconds(self) -> float:
        return sum(cut.span.seconds for cut in self.cuts)


def overlap(first: Range, second: Range) -> float:
    if first.source != second.source or first.seconds <= 0 or second.seconds <= 0:
        return 0
    shared = max(0, min(first.end, second.end) - max(first.start, second.start))
    return shared / min(first.seconds, second.seconds)


def or_parent(span: Range, sources: dict[str, Source]) -> Range:
    source = sources[span.source]
    if source.parent and source.parent_offset is not None:
        return Range(source.parent, span.start + source.parent_offset, span.end + source.parent_offset)
    return span


def same_footage(first: str, second: str) -> bool:
    if not first or not second:
        return False
    try:
        pairs = zip(first.split("."), second.split("."), strict=True)
        distances = [(int(a, 16) ^ int(b, 16)).bit_count() for a, b in pairs]
    except ValueError:
        return False
    return len(distances) == 3 and max(distances) <= 9 and sum(distances) <= 16


def duplicate(first: Moment, second: Moment, sources: dict[str, Source]) -> bool:
    return (overlap(or_parent(first.span, sources), or_parent(second.span, sources)) >= .45
            or same_footage(first.fingerprint, second.fingerprint))
