from __future__ import annotations

import json
from collections import Counter, defaultdict

from .core import Cut, Edit, Moment, Range, Sequence, Source, clean, number, or_parent, overlap, same_footage
from .editorial import GROUP_EDIT, SEQUENCE_EDIT, bounded, diversify, platform_rotation
from .gemini import NUM, TEXT, Gemini, QuotaError, array, object_schema
from .media import Downloads, MediaError, window_proxy

RANK_CRITERIA = {
    "chase": "difficulty, car control, how close pursuit gets, and decisiveness of the escape",
    "driving": "car control, scenery, and how memorable each ride is",
    "stunt": "difficulty, precision, and the visible landing or outcome",
    "theory": "strength of the observable clues and the specificity of the theory",
    "podcast": "clarity of the claim, quality of the response, and the payoff",
    "comedy": "setup, timing, and how clearly the punchline lands",
    "radio": "how well the in-game audio fits the moment, supported by the footage",
}
COMPACT = object_schema({"cuts": array(object_schema({"start": NUM, "end": NUM, "reason": TEXT}), 2)})


class UnavailableMoment(MediaError):
    def __init__(self, moment: Moment, error: MediaError):
        super().__init__(f"{moment.span.source} at {moment.span.start:.2f}s: {error}")
        self.moment_id = moment.identity


def previous_ranges(ledger: dict, sources: dict[str, Source]) -> list[Range]:
    ranges = []
    for edit in ledger.get("edits", []):
        for row in edit["ranges"]:
            span = Range(**row)
            if span.seconds > 0 and span.source in sources:
                ranges.append(or_parent(span, sources))
    return ranges


def is_new(span: Range, prior: list[Range], sources: dict[str, Source]) -> bool:
    return not any(overlap(or_parent(span, sources), old) >= .55 for old in prior)


def single_moment(moment: Moment) -> Edit:
    context = f"Opening: {moment.hook} Setup: {moment.setup} Payoff: {moment.payoff} Evidence: {moment.evidence}"
    return Edit([Cut(moment.span, fingerprint=moment.fingerprint)], moment.theme, moment.hook, context, moment.gta_vi_evidence)


def compact_for_rank(gemini: Gemini, downloads: Downloads, item: Moment, source: Source) -> Range:
    if item.span.seconds <= 25:
        return item.span
    media = window_proxy(downloads, source, item.span)
    reply = gemini.json(
        f"Suggest a coherent 10–27 second continuous extract for one countdown entry. "
        f"Keep the visible opening and payoff. The expected payoff, to verify, is {item.payoff!r}. "
        "Use seconds from the start of the supplied video. If shortening would ruin the story, return no options.",
        COMPACT, [gemini.video(media, fps=3)],
    )
    for row in reply["cuts"]:
        try:
            span = bounded(row, item.span.source, item.span)
            if 10 <= span.seconds <= 27 and clean(row["reason"], 120):
                return span
        except (KeyError, TypeError, ValueError):
            continue
    return item.span


def expand_for_themed(gemini: Gemini, downloads: Downloads, item: Moment, source: Source) -> Range:
    window = Range(source.key, max(0, item.span.start - 18), min(source.duration, item.span.end + 20))
    media = window_proxy(downloads, source, window)
    prompt = (
        "This will be one beat in a horizontal GTA VI themed compilation. Select a continuous cut that keeps the "
        "core highlight and gives a new viewer more useful setup or aftermath when present. Skip filler. "
        f"The scout estimated the core event is {item.span.start - window.start:.1f}–{item.span.end - window.start:.1f}s "
        f"and the payoff is {item.payoff!r}. Verify directly. Use seconds from the supplied video. "
        "Return no cuts if the current context is already enough."
    )
    reply = gemini.json(prompt, COMPACT, [gemini.video(media, fps=2)])
    for row in reply["cuts"]:
        try:
            expanded = bounded(row, source.key, window)
            if 12 <= expanded.seconds <= 80 and overlap(expanded, item.span) >= .85 and clean(row["reason"], 120):
                return expanded
        except (KeyError, TypeError, ValueError):
            continue
    return item.span


def group_plan(gemini: Gemini, downloads: Downloads, group: list[Moment], sources: dict[str, Source],
               ranked: bool, youtube_count: int) -> tuple[Edit, set[str]] | None:
    theme = group[0].theme
    criteria = RANK_CRITERIA.get(theme, "difficulty, clarity, and memorability of the visible payoff")
    ordering = (
        f"Choose exactly 3 comparable entries and order them #3, #2, #1 using {criteria}. "
        f"Use {youtube_count} YouTube and {3 - youtube_count} Twitch entries. "
        "Open on the stakes or payoff setup of #3. Each entry needs its own standout payoff. "
        "The viewer should be able to see why #1 wins. "
        "Labels are short editorial captions."
        if ranked else
        "Choose 4 or 6 entries with equal YouTube and Twitch counts. Start with a strong scene and build "
        "one specific premise through distinct developments or examples. Leave out repetitive entries. "
        "Each label should name the scene in at most six words."
    )
    briefs = [{"id": item.identity, "platform": sources[item.span.source].site,
               "hook": item.hook, "setup": item.setup, "payoff": item.payoff,
               "evidence": item.evidence, "seconds": round(item.span.seconds)} for item in group]
    reply = gemini.json(
        f"Plan one GTA VI video about {theme}. {ordering} Return no entries if the moments do not support a coherent edit. "
        "Keep the premise specific and supported by the briefs. Return entry IDs verbatim.\n" + json.dumps(briefs),
        GROUP_EDIT,
    )
    entries = reply["entries"]
    table = {item.identity: item for item in group}
    ids = {entry["id"] for entry in entries}
    if len(entries) != len(ids) or not ids <= table.keys():
        return None
    youtube = sum(sources[table[key].span.source].site == "youtube" for key in ids)
    if ranked and (len(ids) != 3 or youtube != youtube_count):
        return None
    if not ranked and (len(ids) not in {4, 6} or youtube * 2 != len(ids)):
        return None
    cuts = []
    for index, entry in enumerate(entries):
        item = table[entry["id"]]
        source = sources[item.span.source]
        try:
            span = (compact_for_rank(gemini, downloads, item, source) if ranked
                    else expand_for_themed(gemini, downloads, item, source))
        except MediaError:
            span = item.span
        try:
            downloads.get(source, span)
        except MediaError as error:
            raise UnavailableMoment(item, error) from None
        cuts.append(Cut(span, clean(entry["label"], 54), len(entries) - index if ranked else None, item.fingerprint))
    proof = " ".join(table[entry["id"]].gta_vi_evidence for entry in entries)
    edit = Edit(cuts, theme, clean(reply["idea"], 180), clean(reply["context"], 600), clean(proof, 1250))
    return (edit, ids) if not ranked or 24 <= edit.seconds <= 88 else None


def group_plans(gemini: Gemini, downloads: Downloads, moments: list[Moment], sources: dict[str, Source],
                report: list[str], ranked: bool):
    by_theme: dict[str, list[Moment]] = defaultdict(list)
    for item in moments:
        by_theme[item.theme].append(item)
    needed = 3 if ranked else 4
    groups = [diversify(group, sources) for group in by_theme.values() if len(group) >= needed]
    groups.sort(key=lambda group: sum(item.score for item in group[:needed]) / needed, reverse=True)
    unavailable = set()
    for available in groups[:5]:
        theme = available[0].theme
        for youtube_count in ((2, 1) if ranked else (2, 2)):
            for _ in range(3):
                readable = [item for item in available if item.identity not in unavailable
                            and item.span.source not in downloads.unreachable]
                group = platform_rotation(readable, sources, lambda item: item.span.source)[:12]
                counts = Counter(sources[item.span.source].site for item in group)
                if counts["youtube"] < youtube_count or counts["twitch"] < needed - youtube_count:
                    break
                try:
                    proposal = group_plan(gemini, downloads, group, sources, ranked, youtube_count)
                    if proposal:
                        edit, used = proposal
                        available = [item for item in available if item.identity not in used]
                        yield edit
                    break
                except UnavailableMoment as error:
                    unavailable.add(error.moment_id)
                    report.append(f"{theme} compilation source unavailable: {error}")
                except QuotaError as error:
                    report.append(f"Compilation planning paused: {error}")
                    return
                except (KeyError, ValueError, TypeError, MediaError) as error:
                    report.append(f"{theme} compilation plan: {error}")
                    break


def sequence_cuts(reply: dict, window: Range, *, retry: bool = False) -> list[Cut]:
    offset = window.start if reply.get("time_basis") == "source" else 0
    cuts = []
    for row in reply["cuts"]:
        try:
            local = ({key: number(row[key], offset, window.end + 1) - offset for key in ("start", "end")}
                     if offset else row)
            span = bounded(local, window.source, window, end_tolerance=1)
            if span.seconds < 5 or cuts and span.start < cuts[-1].span.end:
                raise ValueError("The edit has undersized, overlapping, or out-of-order cuts.")
            cuts.append(Cut(span, clean(row["beat"], 68)))
        except (KeyError, TypeError, ValueError):
            if not retry:
                raise
    return cuts


def condensed_plan(gemini: Gemini, downloads: Downloads, sequence: Sequence, source: Source) -> Edit | None:
    media = window_proxy(downloads, source, sequence.span, timecode=True)
    prompt = (
        f"Edit one complete chronological GTA VI sequence within this {sequence.span.seconds:.1f}s excerpt. "
        f"The initial scout's story was {sequence.story!r}; confirm and correct it using the source. "
        "Keep a strong opening, required dialogue and cause/effect, escalation, and an ending viewers can understand. "
        "Keep transitions that explain how the next beat happens. Remove loading, idle travel, menus, routine interactions, "
        "and repeated attempts unless a specific detail changes the stakes or advances the story. "
        "Read the elapsed seconds clock in the top left for every boundary. "
        f"Return at most ten ordered, non-overlapping ranges using that clock, with time_basis set to excerpt. "
        f"The first frame is 0 and the end is {sequence.span.seconds:.2f}s. "
        "Keep at least 240 seconds total with an honest payoff. Empty cuts are valid if there is too little real material. "
        "Label each kept range with its narrative beat. The edit idea should be a specific viewable premise."
    )
    video = gemini.video(media, fps=1.5)
    reply = gemini.json(prompt, SEQUENCE_EDIT, [video])
    try:
        cuts = sequence_cuts(reply, sequence.span)
    except (KeyError, TypeError, ValueError):
        reply = gemini.json(prompt + (
            f" The earlier ranges were invalid. Read the clock shown in the video and use "
            f"ordered ranges within 0–{sequence.span.seconds:.2f}s. Return no cuts if you cannot locate the scenes."
        ), SEQUENCE_EDIT, [video])
        cuts = sequence_cuts(reply, sequence.span, retry=True)
    if not cuts:
        return None
    edit = Edit(cuts, sequence.theme, clean(reply["idea"], 180), clean(reply["story"], 520))
    return edit if edit.seconds >= 240 else None


def plans(mode: str, gemini: Gemini, downloads: Downloads, catalog: dict, ledger: dict,
          sources: dict[str, Source], report: list[str], rejected: dict):
    earlier = previous_ranges(ledger, sources)
    if mode == "condensed":
        sequences = [Sequence.from_dict(row) for row in catalog["sequences"] if row["span"]["source"] in sources
                     and is_new(Range(**row["span"]), earlier, sources)]
        ordered = platform_rotation(sequences, sources, lambda item: item.span.source)
        attempts = 0
        for sequence in ordered:
            if sequence.span.source in downloads.unreachable:
                continue
            if attempts >= 16:
                break
            attempts += 1
            try:
                edit = condensed_plan(gemini, downloads, sequence, sources[sequence.span.source])
                if edit:
                    yield edit
            except QuotaError as error:
                report.append(f"Condensed planning paused: {error}")
                return
            except (KeyError, ValueError, TypeError, MediaError) as error:
                report.append(f"{sequence.span.source} condensed plan: {error}")
        return
    moments = [Moment.from_dict(row) for row in catalog["moments"]
               if row["span"]["source"] in sources and is_new(Range(**row["span"]), earlier, sources)]
    published = {fp for edit in ledger.get("edits", []) for fp in edit.get("fingerprints", [])}
    moments = [item for item in moments if not any(same_footage(item.fingerprint, fp) for fp in published)]
    if mode == "shorts":
        primary, reserve = [], []
        for moment in diversify(moments, sources):
            options = [single_moment(cut) for cut in (moment, *moment.alternate_cuts)
                       if is_new(cut.span, earlier, sources)]
            options = [edit for edit in options if edit.identity not in rejected]
            if options:
                primary.append(options[0])
                reserve.extend(options[1:])
        yield from platform_rotation(primary + reserve, sources, lambda edit: edit.cuts[0].span.source)
    else:
        yield from group_plans(gemini, downloads, moments, sources, report, mode == "ranked")
