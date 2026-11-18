from __future__ import annotations

import json
from collections import defaultdict

from .core import Cut, Edit, Moment, Range, Sequence, Source, clean, or_parent, overlap
from .editorial import GROUP_EDIT, SEQUENCE_EDIT, bounded, diversify
from .gemini import NUM, TEXT, Gemini, array, object_schema
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
    return Edit([Cut(moment.span)], moment.theme, moment.hook, context)


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


def group_plans(gemini: Gemini, downloads: Downloads, moments: list[Moment], sources: dict[str, Source],
                report: list[str], ranked: bool):
    by_theme: dict[str, list[Moment]] = defaultdict(list)
    for item in moments:
        by_theme[item.theme].append(item)
    needed = 3 if ranked else 4
    groups = [diversify(group, sources) for group in by_theme.values() if len(group) >= needed]
    groups.sort(key=lambda group: sum(item.score for item in group[:needed]) / needed, reverse=True)
    for group in groups[:5]:
        group = group[:12]
        theme = group[0].theme
        criteria = RANK_CRITERIA.get(theme, "difficulty, clarity, and memorability of the visible payoff")
        ordering = (
            f"Choose exactly 3 comparable entries and order them #3, #2, #1 using {criteria}. "
            "Each entry needs its own standout payoff. The viewer should be able to see why #1 wins. "
            "Labels are short editorial captions."
            if ranked else
            "Choose 4–6 entries that support one specific premise. Start with a strong scene and build through "
            "distinct developments or examples. Leave out repetitive entries and return no entries if the remaining "
            "scenes do not support the premise. Each label should name the scene in at most six words."
        )
        briefs = [{"id": item.identity, "hook": item.hook, "setup": item.setup, "payoff": item.payoff,
                   "evidence": item.evidence, "seconds": round(item.span.seconds)} for item in group]
        try:
            reply = gemini.json(
                f"Plan one GTA VI video about {theme}. {ordering} Skip the group if these moments do not belong together. "
                "Keep the premise specific and supported by the briefs. Return entry IDs verbatim.\n" + json.dumps(briefs),
                GROUP_EDIT,
            )
            entries = reply["entries"]
            table = {item.identity: item for item in group}
            ids = [entry["id"] for entry in entries]
            if len(ids) != len(set(ids)) or any(value not in table for value in ids):
                continue
            if ranked and len(ids) != 3 or not ranked and not 4 <= len(ids) <= 6:
                continue
            cuts = []
            for index, entry in enumerate(entries):
                item = table[entry["id"]]
                source = sources[item.span.source]
                if ranked:
                    span = compact_for_rank(gemini, downloads, item, source)
                else:
                    span = expand_for_themed(gemini, downloads, item, source)
                cuts.append(Cut(span, clean(entry["label"], 54), len(entries) - index if ranked else None))
            edit = Edit(cuts, theme, clean(reply["idea"], 180), clean(reply["context"], 600))
            if not ranked or 24 <= edit.seconds <= 88:
                yield edit
        except (KeyError, ValueError, TypeError, MediaError) as error:
            report.append(f"{theme} compilation plan: {error}")


def condensed_plan(gemini: Gemini, downloads: Downloads, sequence: Sequence, source: Source) -> Edit | None:
    media = window_proxy(downloads, source, sequence.span)
    prompt = (
        f"Edit one complete chronological GTA VI sequence within this {sequence.span.seconds:.1f}s excerpt. "
        f"The initial scout's story was {sequence.story!r}; confirm and correct it using the source. "
        "Keep a strong opening, required dialogue and cause/effect, escalation, and an ending viewers can understand. "
        "Keep transitions that explain how the next beat happens. Remove loading, idle travel, menus, routine interactions, "
        "and repeated attempts unless a specific detail changes the stakes or advances the story. "
        "Return ordered non-overlapping ranges, at most ten, using seconds from this excerpt. "
        "Keep at least 240 seconds total with an honest payoff. Empty cuts are valid if there is too little real material. "
        "Label each kept range with its narrative beat. The edit idea should be a specific viewable premise."
    )
    reply = gemini.json(prompt, SEQUENCE_EDIT, [gemini.video(media, fps=1.5)])
    cuts: list[Cut] = []
    for row in reply["cuts"]:
        span = bounded(row, source.key, sequence.span)
        if span.seconds < 5 or cuts and span.start < cuts[-1].span.end:
            return None
        cuts.append(Cut(span, clean(row["beat"], 68)))
    if not cuts:
        return None
    edit = Edit(cuts, sequence.theme, clean(reply["idea"], 180), clean(reply["story"], 520))
    return edit if edit.seconds >= 240 else None


def plans(mode: str, gemini: Gemini, downloads: Downloads, catalog: dict, ledger: dict,
          sources: dict[str, Source], report: list[str]):
    earlier = previous_ranges(ledger, sources)
    if mode == "condensed":
        sequences = [Sequence.from_dict(row) for row in catalog["sequences"] if row["span"]["source"] in sources]
        for sequence in sequences[:12]:
            if not is_new(sequence.span, earlier, sources):
                continue
            try:
                edit = condensed_plan(gemini, downloads, sequence, sources[sequence.span.source])
                if edit:
                    yield edit
            except (KeyError, ValueError, TypeError, MediaError) as error:
                report.append(f"{sequence.span.source} condensed plan: {error}")
        return
    moments = [Moment.from_dict(row) for row in catalog["moments"]
               if row["span"]["source"] in sources and is_new(Range(**row["span"]), earlier, sources)]
    if mode == "shorts":
        yield from (single_moment(moment) for moment in diversify(moments, sources))
    else:
        yield from group_plans(gemini, downloads, moments, sources, report, mode == "ranked")
