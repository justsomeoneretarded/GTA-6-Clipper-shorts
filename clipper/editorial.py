from __future__ import annotations

import json
import os
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import date

from .core import (THEMES, VERSION, WEIGHTS, Cut, Edit, Moment, Range, Sequence, Source, clean,
                   dated, digest, duplicate, number, overlap, or_parent, source_cutoff, timestamp)
from .discovery import priority
from .gemini import BOOL, NUM, TEXT, Gemini, QuotaError, array, object_schema, search_enabled
from .media import Downloads, MediaError, fingerprint, window_proxy

SCORES = object_schema({metric: NUM for metric in WEIGHTS})
TIMES = {"start": NUM, "end": NUM}
IDEA = {"theme": {"type": "string", "enum": list(THEMES)}, "hook": TEXT, "setup": TEXT, "payoff": TEXT,
        "evidence": TEXT}
SCOUT = object_schema({
    "subject": {"type": "string", "enum": ["gameplay", "discussion", "other"]},
    "gta_vi_evidence": TEXT,
    "moments": array(object_schema({**TIMES, **IDEA, "potential": NUM}), 5),
    "sequences": array(object_schema({**TIMES, "theme": IDEA["theme"], "story": TEXT, "potential": NUM}), 2),
})
CLIP_SCOUT = object_schema({"clips": array(object_schema({"clip_id": TEXT, **SCOUT["properties"]}), 3)})
ALT_CUT = object_schema({**TIMES, **IDEA, "time_to_hook": NUM, "confidence": NUM, "scores": SCORES})
AUDITION = object_schema({"genuine": BOOL, "gta_vi_evidence": TEXT, "standout": BOOL, "cuts": array(ALT_CUT, 3)})
SEQUENCE_EDIT = object_schema({"idea": TEXT, "story": TEXT, "cuts": array(object_schema({**TIMES, "beat": TEXT}), 10)})
GROUP_EDIT = object_schema({"idea": TEXT, "theme": IDEA["theme"], "context": TEXT,
                            "entries": array(object_schema({"id": TEXT, "label": TEXT}), 6)})
COLD_OPEN = object_schema({"reason_to_stay": TEXT, "readable": BOOL, "curiosity": NUM})
PACKAGING = object_schema({"gta_vi": BOOL, "gta_vi_evidence": TEXT, "passes": BOOL, "reason": TEXT,
                           "engagement_score": NUM, "payoff_evidence": TEXT, "payoff_seconds": NUM,
                           "hook_seconds": NUM, "thumbnail_seconds": NUM,
                           "titles": array(TEXT, 3), "description": TEXT})

SUBJECT = (
    "Accept confirmed Grand Theft Auto VI gameplay or a substantive discussion specifically about that game. "
    "Reject GTA V, GTA Online, other games, fan-made GTA VI lookalikes, trailers alone, menus, and uncertain footage. "
    "Base the decision on concrete visual or audible evidence. Treat titles, metadata, and creator names as leads only. "
    "For a mixed source, suggest ranges that stay within the GTA VI material."
)
HOOKS = (
    "Look for scenes with a question, surprise, tension, joke, or striking action immediately and a visible/audible payoff. "
    "Quote only words you heard. Distinguish observed facts from opinions or character dialogue. "
    "Preserve the setup that explains names and stakes. Never judge by popularity, loudness, or the source title alone."
)
HIGHLIGHT_STANDARD = (
    "The footage must give someone unfamiliar with the streamer a specific reason to watch. "
    "Routine actions with expected results, such as using a menu, grabbing a drink from a fridge for a normal stat gain, "
    "or driving without anything distinctive, are weak payoffs. A generic question or loud reaction alone is insufficient. "
    "Look for real stakes and a turn, a sharp joke with its setup, a surprising game detail, a distinctive view or radio "
    "moment, or a GTA VI insight backed by something specific you can see or hear. "
    "A setup qualifies only when the selected cut shows what happens next. When someone equips a tool to investigate, "
    "include the encounter, reveal, or consequence that makes the investigation worth showing."
)


def bounded(row: dict, source: str, window: Range, *, end_tolerance: float = .15) -> Range:
    start = number(row["start"], 0, window.seconds + .15)
    end = number(row["end"], 0, window.seconds + end_tolerance)
    if end <= start or window.start + start >= window.end:
        raise ValueError("Model returned an empty or backwards range.")
    return Range(source, round(window.start + start, 3), round(min(window.end, window.start + end), 3))


def scout_windows(source: Source) -> list[Range]:
    if source.duration <= 420:
        return [Range(source.key, 0, source.duration)]
    tail = source.duration - 420
    span = int(digest(source.key, date.today().isoformat()), 16)
    rotating = .25 + (span % 500) / 1000
    positions = [max(0, min(tail, anchor - 145)) for anchor in source.anchors[:2]]
    positions.extend([min(70, tail * .12), tail * rotating, tail * .83])
    windows: list[Range] = []
    for offset in positions:
        window = Range(source.key, round(offset, 2), round(offset + 420, 2))
        if not any(overlap(window, previous) >= .67 for previous in windows):
            windows.append(window)
    return windows


def platform_rotation[T](items: list[T], sources: dict[str, Source],
                         source_key: Callable[[T], str]) -> list[T]:
    youtube: deque[T] = deque()
    twitch: deque[T] = deque()
    for item in items:
        (youtube if sources[source_key(item)].site == "youtube" else twitch).append(item)
    ordered = []
    while youtube or twitch:
        for queue in (youtube, twitch):
            if queue:
                ordered.append(queue.popleft())
    return ordered


def width_rotation(sources: dict[str, Source], used: dict[str, str]) -> list[Range]:
    counts: Counter[str] = Counter()
    lists: list[list[Range]] = []
    for source in sorted(sources.values(), key=priority, reverse=True):
        if source.duration <= 0:
            continue
        creator = source.creator or source.key
        if counts[creator] >= 4:
            continue
        windows = [window for window in scout_windows(source) if window.identity not in used]
        if windows:
            counts[creator] += 1
            lists.append(windows)
    # Round-robin coverage; reserve slots for both longer archives and existing Twitch clips.
    result = []
    for index in range(max((len(windows) for windows in lists), default=0)):
        batch = [windows[index] for windows in lists if len(windows) > index]
        groups = [[window for window in batch if sources[window.source].platform == platform]
                  for platform in ("youtube", "twitch-clip", "twitch-vod")]
        while any(groups):
            result.extend(group.pop(0) for group in groups if group)
    opening = result[:12]
    seen = {window.identity for window in opening}
    extra = [window for windows in lists[:12] for window in windows[1:] if window.identity not in seen]
    opening += extra[::2][:8]
    seen.update(window.identity for window in opening)
    ordered = opening + [window for window in result if window.identity not in seen]
    return platform_rotation(ordered, sources, lambda window: window.source)


def scouting_batches(windows: list[Range], sources: dict[str, Source]) -> list[list[Range]]:
    clips = deque(window for window in windows
                  if sources[window.source].platform == "twitch-clip" and window.seconds <= 60)
    clip_ids = {window.identity for window in clips}
    queued = set()
    batches = []
    for window in windows:
        if window.identity in queued:
            continue
        batch = ([clips.popleft() for _ in range(min(3, len(clips)))]
                 if window.identity in clip_ids else [window])
        queued.update(span.identity for span in batch)
        batches.append(batch)
    return platform_rotation(batches, sources, lambda batch: batch[0].source)


def score_cut(row: dict, floor: float) -> float:
    scores = {name: number(row["scores"][name], 0, 10) for name in WEIGHTS}
    if scores["hook"] < 7 or scores["payoff"] < 8 or scores["context"] < 6:
        raise ValueError("The opening, payoff, or viewer context is too weak.")
    if number(row["confidence"], 0, 1) < .65 or number(row["time_to_hook"], 0, 60) > 3:
        raise ValueError("The opening is too slow or the model lacks evidence.")
    score = sum(scores[name] * weight * 10 for name, weight in WEIGHTS.items())
    if score < floor:
        raise ValueError("The proposed cut falls below the editorial minimum.")
    return round(score, 2)


def scout(gemini: Gemini, downloads: Downloads, source: Source, window: Range, *, sequence_only: bool = False) -> dict:
    assignment = (
        "Return an empty moments list and focus on sustained scenes for a chronological edit."
        if sequence_only else "Suggest up to five different 10–58s standalone moments."
    )
    prompt = (
        f"Scout this {window.seconds:.1f}s excerpt at source time {timestamp(window.start)}. "
        "Every start/end you report must be seconds FROM THE START OF THIS EXCERPT (0 = first frame provided). "
        f"Source metadata, for orientation only: {source.creator!r}, {source.title[:200]!r}. "
        f"{SUBJECT} {HOOKS} {HIGHLIGHT_STANDARD} {assignment} "
        "State the visual or audible GTA VI evidence in gta_vi_evidence. Use subject other if you cannot confirm it. "
        "For each cut name the opening, needed setup, payoff and an observable frame or line as evidence. "
        "Use the theme that best describes it. Potential is a 0–100 estimate. "
        "If the excerpt covers at least 4.4 minutes, also find up to two coherent sustained sequences of at least "
        "260 seconds with enough meaningful beats for a 4-minute chronological edit. Describe the setup, what changes, "
        "and the visible or audible outcome. Omit long stretches that would rely on idle footage to make the runtime. "
        "An empty list is a valid result."
    )
    if source.platform == "youtube":
        try:
            return gemini.json(prompt, SCOUT, [gemini.youtube(source.url, window.start, window.end)])
        except ValueError:
            pass
    media = window_proxy(downloads, source, window)
    return gemini.json(prompt, SCOUT, [gemini.video(media, fps=1.5)])


def scout_clips(gemini: Gemini, downloads: Downloads, sources: dict[str, Source], windows: list[Range],
                report: list[str]) -> list[tuple[Range, dict]]:
    prepared = []
    media = []
    for window in windows:
        try:
            proxy = window_proxy(downloads, sources[window.source], window)
            video = gemini.video(proxy, fps=2)
        except (MediaError, ValueError) as error:
            report.append(f"{window.source} scouting {timestamp(window.start)}: {error}")
            continue
        media.append(video)
        prepared.append(window)
    if not prepared:
        return []
    clips = {str(index): window for index, window in enumerate(prepared, 1)}
    lengths = ", ".join(f"{key} ({window.seconds:.1f}s)" for key, window in clips.items())
    reply = gemini.json(
        f"The attached clips are separate inputs in this order: {lengths}. Review each on its own. "
        f"{SUBJECT} {HOOKS} {HIGHLIGHT_STANDARD} "
        "Return one result per input using its numbered clip_id. Use seconds from the start of that clip. "
        "For each result identify the subject and directly observed game evidence. Propose up to two distinct "
        "10–58 second highlights with setup and payoff. Leave sequences empty and return no moments for a weak clip.",
        CLIP_SCOUT, media,
    )
    results = {row["clip_id"]: row for row in reply["clips"]}
    if len(reply["clips"]) != len(clips) or results.keys() != clips.keys():
        raise ValueError("Gemini did not identify each scouting clip separately.")
    return [(window, results[key]) for key, window in clips.items()]


def audition(gemini: Gemini, downloads: Downloads, source: Source, lead: Moment, floor: float) -> Moment | None:
    window = Range(source.key, max(0, lead.span.start - 14), min(source.duration, lead.span.end + 15))
    media = window_proxy(downloads, source, window, fps=12)
    candidate = lead.span.start - window.start
    end = lead.span.end - window.start
    prompt = (
        f"Independently verify the proposed highlight around {candidate:.2f}–{end:.2f} seconds in this local excerpt. "
        f"An initial scout thought this was the payoff: {lead.payoff!r}. Correct it from the evidence. "
        f"{SUBJECT} Return genuine false if GTA VI cannot be confirmed and state the evidence in gta_vi_evidence. "
        f"{HOOKS} {HIGHLIGHT_STANDARD} Set standout true only when the event itself delivers that value. "
        "If it is routine or the payoff is unclear, set standout false and return no cuts. "
        "Find up to three alternative openings for the SAME event, for example the first action, "
        "a line that raises the stakes, or a quicker beginning. Each cut must contain the payoff and preserve cause and effect. "
        "Score each 0–10 on hook (reason to stay by second 3), payoff, context, specificity, pace, and readability on a phone. "
        "A payoff of 6–7 is mildly interesting. Reserve 8 or higher for a specific, well-supported standout moment. "
        "Report actual seconds from the start of this excerpt, time_to_hook from the start of that cut, "
        "and confidence 0–1. Reject slow openings, unseen payoffs, unrelated material, and inaccurate claims."
    )
    reply = gemini.json(prompt, AUDITION, [gemini.video(media, fps=4)])
    options = []
    if (reply["genuine"] is not True or reply["standout"] is not True
            or not clean(reply["gta_vi_evidence"], 500)):
        return None
    for row in reply["cuts"]:
        try:
            span = bounded(row, source.key, window)
            score = score_cut(row, floor)
            if not 10 <= span.seconds <= 58 or overlap(span, lead.span) < .40:
                continue
            if not all(clean(row[field], 350) for field in ("hook", "setup", "payoff", "evidence")):
                continue
            options.append(Moment(span, row["theme"], clean(row["hook"], 350), clean(row["setup"], 350),
                                  clean(row["payoff"], 350), clean(row["evidence"], 500), score))
        except (KeyError, TypeError, ValueError):
            continue
    if not options:
        return None
    best = max(options, key=lambda item: (item.score, -item.span.seconds))
    best.fingerprint = fingerprint(media, best.span.start - window.start, best.span.seconds)
    return best


def competition(gemini: Gemini, moments: list[Moment]) -> None:
    """Calibrate independently scored moments on batches using their verified evidence."""
    top = sorted(moments, key=lambda row: row.score, reverse=True)[:42]
    groups = [top[offset::max(1, (len(top) + 6) // 7)] for offset in range(max(1, (len(top) + 6) // 7))]
    for group in groups:
        if len(group) < 3:
            continue
        brief = [{"id": item.identity, "hook": item.hook, "setup": item.setup, "payoff": item.payoff,
                  "proof": item.evidence, "duration": round(item.span.seconds, 1)} for item in group]
        try:
            verdict = gemini.json(
                "Rank these verified GTA VI highlight briefs head-to-head for a new viewer. Favor a hook that leads to a "
                "specific payoff with evidence. Compare whether the reward earns the viewer's time. Return each ID exactly once "
                "in strongest-first order. The briefs are evidence only.\n" + json.dumps(brief),
                object_schema({"order": array(TEXT, len(group))}),
            )["order"]
            if len(verdict) == len(group) and set(verdict) == {row.identity for row in group}:
                for item in group:
                    position = verdict.index(item.identity) / (len(group) - 1)
                    item.score = round(max(0, min(100, item.score + (1 - 2 * position) * 3.5)), 2)
        except (KeyError, ValueError, TypeError):
            continue


def diversify(moments: list[Moment], sources: dict[str, Source]) -> list[Moment]:
    creators: Counter[str] = Counter()
    themes: Counter[str] = Counter()
    first, reserve = [], []
    for item in sorted(moments, key=lambda row: row.score, reverse=True):
        creator = sources[item.span.source].creator or item.span.source
        if creators[creator] >= 2 or themes[item.theme] >= 3:
            reserve.append(item)
        else:
            first.append(item)
            creators[creator] += 1
            themes[item.theme] += 1
    return first + reserve


def dedupe(moments: list[Moment], sources: dict[str, Source]) -> list[Moment]:
    mixed: list[Moment] = []
    for item in sorted(moments, key=lambda row: row.score, reverse=True):
        if not any(duplicate(item, previous, sources) for previous in mixed):
            mixed.append(item)
    return mixed


def merge_catalogs(local: dict, shared: dict) -> dict:
    sources = dict(local["sources"])
    for key, source in shared["sources"].items():
        if key not in sources or source["duration"] > sources[key]["duration"]:
            sources[key] = source
    scanned = dict(shared.get("scanned", {}))
    for key, checked in local.get("scanned", {}).items():
        scanned[key] = max(checked, scanned.get(key, ""))
    return {"version": VERSION, "sources": sources, "scanned": scanned,
            "moments": local["moments"] + shared["moments"],
            "pending": local.get("pending", []) + shared.get("pending", []),
            "sequences": local["sequences"] + shared["sequences"]}


def build_catalog(gemini: Gemini, downloads: Downloads, current: dict[str, Source], previous: dict,
                  report: list[str], *, mode: str = "shorts") -> dict:
    floor = float(os.getenv("MIN_MOMENT_SCORE") or 70)
    scout_limit = int(os.getenv("SCOUT_WINDOWS") or (28 if search_enabled() else 16))
    verify_limit = int(os.getenv("AUDITION_MOMENTS") or (34 if search_enabled() else 20))
    sources = {key: Source(**row) for key, row in previous.get("sources", {}).items()}
    for key, row in current.items():
        if row.published or key not in sources or not sources[key].published:
            sources[key] = row
    oldest_source = source_cutoff(21)
    sources = {key: row for key, row in sources.items() if row.published and row.published >= oldest_source}
    scanned = {key: value for key, value in previous.get("scanned", {}).items() if value >= dated(10)}
    sequence_only = mode == "condensed"
    leads = [] if sequence_only else [Moment.from_dict(row) for row in previous.get("pending", [])
                                     if row["span"]["source"] in sources]
    new_sequences: list[Sequence] = []
    limited = False
    scout_sources = {key: sources[key] for key in current if key in sources
                     and (not sequence_only or sources[key].duration >= 260)}
    windows = width_rotation(scout_sources, scanned)
    batches = deque(scouting_batches(windows, sources)[:scout_limit * 2])
    queued_windows = sum(len(batch) for batch in batches)
    scouting_slots: Counter[str] = Counter()
    reviewed: Counter[str] = Counter()
    print(f"Discovered {len(current)} sources. Up to {queued_windows} windows are queued for scouting.", flush=True)
    for index in range(scout_limit * 2):
        if not batches or sum(scouting_slots.values()) >= scout_limit:
            break
        batch = batches.popleft()
        window = batch[0]
        site = sources[window.source].site
        scouting_slots[site] += 1
        print(f"Scouting {index + 1}: " + ", ".join(f"{span.source} at {timestamp(span.start)}" for span in batch),
              flush=True)
        try:
            if len(batch) == 1:
                replies = [(window, scout(gemini, downloads, sources[window.source], window, sequence_only=sequence_only))]
            else:
                replies = scout_clips(gemini, downloads, sources, batch, report)
            if not replies:
                scouting_slots[site] -= 1
            for window, reply in replies:
                item = sources[window.source]
                if reply["subject"] in {"gameplay", "discussion"} and clean(reply["gta_vi_evidence"], 500):
                    for row in () if sequence_only else reply["moments"]:
                        try:
                            span = bounded(row, item.key, window)
                            potential = number(row["potential"], 0, 100)
                            if 9 <= span.seconds <= 60 and potential >= 56 and row["theme"] in THEMES:
                                leads.append(Moment(span, row["theme"], clean(row["hook"], 350), clean(row["setup"], 350),
                                                    clean(row["payoff"], 350), clean(row["evidence"], 500), potential))
                        except (KeyError, TypeError, ValueError):
                            continue
                    for row in reply["sequences"]:
                        try:
                            span = bounded(row, item.key, window)
                            potential = number(row["potential"], 0, 100)
                            if span.seconds >= 260 and potential >= 66 and row["theme"] in THEMES:
                                new_sequences.append(Sequence(span, row["theme"], clean(row["story"], 550), potential))
                        except (KeyError, TypeError, ValueError):
                            continue
                scanned[window.identity] = dated()
                reviewed[site] += 1
        except QuotaError as error:
            report.append(f"Scouting paused: {error}")
            limited = True
            break
        except MediaError as error:
            if site == "twitch":
                # Twitch media is prepared before the model call.
                scouting_slots[site] -= 1
            report.append(f"{window.source} scouting {timestamp(window.start)}: {error}")
        except (KeyError, ValueError, TypeError) as error:
            report.append(f"{window.source} scouting {timestamp(window.start)}: {error}")
            if len(batch) > 1:
                pending_windows = [span for group in (batch, *batches) for span in group if span.identity not in scanned]
                batches = deque([span] for span in platform_rotation(pending_windows, sources, lambda row: row.source))
                report.append("Continuing with one clip at a time after the grouped review failed.")
    report.append(f"Scouting slots used — YouTube {scouting_slots['youtube']}, Twitch {scouting_slots['twitch']}.")
    report.append(f"Excerpts reviewed — YouTube {reviewed['youtube']}, Twitch {reviewed['twitch']}.")
    previous_moments = [] if sequence_only else [Moment.from_dict(row) for row in previous.get("moments", [])
                                                if row["span"]["source"] in sources]
    verified: list[Moment] = []
    distinct_leads = [item for item in dedupe(leads, sources) if not any(duplicate(item, old, sources) for old in previous_moments)]
    auditions = [] if limited else platform_rotation(diversify(distinct_leads, sources), sources,
                                                     lambda item: item.span.source)[:verify_limit]
    attempted: set[str] = set()
    for index, lead in enumerate(auditions, 1):
        print(f"Audition {index}/{len(auditions)}: {lead.span.source} at {timestamp(lead.span.start)}", flush=True)
        try:
            checked = audition(gemini, downloads, sources[lead.span.source], lead, floor)
            if checked:
                verified.append(checked)
        except QuotaError as error:
            report.append(f"Auditions paused: {error}")
            limited = True
            break
        except (KeyError, ValueError, TypeError, MediaError) as error:
            report.append(f"{lead.span.source} audition {timestamp(lead.span.start)}: {error}")
        attempted.add(lead.identity)
    if not limited:
        try:
            competition(gemini, verified)
        except QuotaError as error:
            report.append(f"Used the individual audition scores: {error}")
    pool = dedupe(verified + previous_moments, sources)[:180]
    pending = [item for item in distinct_leads if item.identity not in attempted
               and not any(duplicate(item, other, sources) for other in pool)][:80]
    sequences = [Sequence.from_dict(row) for row in previous.get("sequences", []) if row["span"]["source"] in sources]
    sequence_pool: list[Sequence] = []
    for sequence in sorted(new_sequences + sequences, key=lambda row: row.score, reverse=True):
        if not any(overlap(sequence.span, other.span) >= .70 for other in sequence_pool):
            sequence_pool.append(sequence)
    sequence_pool = sequence_pool[:55]
    referenced = {item.span.source for item in [*pool, *pending, *sequence_pool]}
    for key in list(referenced):
        if sources[key].parent in sources:
            referenced.add(sources[key].parent)
    report.append(f"Discovery found {len(current)} sources. There were {len(leads)} proposed moments, including saved candidates. "
                  f"Auditions verified {len(verified)} new moments, with {len(pending)} saved for later. "
                  f"The catalog contains {len(sequence_pool)} longer sequences.")
    return {"version": VERSION, "generated_at": dated(), "scanned": scanned,
            "sources": {key: asdict(sources[key]) for key in sorted(referenced)},
            "moments": [item.to_dict() for item in pool],
            "pending": [item.to_dict() for item in pending],
            "sequences": [item.to_dict() for item in sequence_pool]}
