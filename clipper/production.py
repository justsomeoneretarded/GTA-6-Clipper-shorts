from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .core import VERSION, Edit, Source, clean, dated, number, or_parent, source_cutoff, source_url, timestamp, write_json
from .editorial import COLD_OPEN, DISCUSSION, HIGHLIGHT_STANDARD, PACKAGING, SUBJECT
from .gemini import Gemini, QuotaError, ServiceError
from .media import (Captions, Downloads, MediaError, ffmpeg, finish_video, join_pieces, proxy, render_piece,
                    verify_video, window_proxy)
from .plans import is_new, plans

REVIEW_BY_MODE = {
    "shorts": "The opening must give a concrete reason to stay, and the single moment must deliver a standout payoff.",
    "ranked": "Each ranked entry must warrant inclusion, the comparisons must be clear, and the #1 result must be earned.",
    "themed": "Each scene must add a distinct development, example, or perspective to the central premise.",
    "condensed": "The sequence must sustain interest through setup, escalation, and resolution. "
                 "Keep necessary connective beats and reject padding used to reach four minutes.",
}
MODES = tuple(REVIEW_BY_MODE)


class EditorialRejection(ValueError):
    """A finished cut that the editor judged on its footage."""


def sources_from(catalog: dict) -> dict[str, Source]:
    if catalog.get("version") != VERSION:
        raise ValueError("This workflow requires a compatible v1 catalog.")
    sources = {}
    for key, row in catalog["sources"].items():
        source = Source(**row)
        parsed = source_url(source.url)
        if not parsed or parsed.key != key or source.key != key or parsed.platform != source.platform:
            raise ValueError("Catalog contains an invalid source URL.")
        sources[key] = source
    return sources


def approved_package(gemini: Gemini, edit: Edit, media: Path, length: float,
                     captions: list[dict], mode: str, credits: list[dict], *, reference: Path | None = None,
                     stages: Counter[str] | None = None) -> dict:
    is_short = mode in {"shorts", "ranked"}
    opening_verdict = {}
    if is_short:
        opening = proxy(media, 0, min(3.3, length), media.with_name("opening.mp4"), fps=12)
        opening_verdict = gemini.json(
            "You are seeing only the opening of a vertical Short, with no title or context. "
            "What specifically makes a first-time viewer want to see the next few seconds? "
            "Point to the image or sound that creates tension, surprise, humor, or curiosity about a specific detail. "
            "A vague question over routine action does not earn interest. Leave reason_to_stay empty if there is no "
            "specific hook. Score curiosity from 0 to 10, with 7 or higher for a clear reason to stay, and mark whether "
            "the focal action or dialogue is readable at phone size.",
            COLD_OPEN, [gemini.video(opening, fps=4)],
        )
        if (opening_verdict["readable"] is not True or not clean(opening_verdict["reason_to_stay"], 300)
                or number(opening_verdict["curiosity"], 0, 10) < 7):
            detail = clean(opening_verdict["reason_to_stay"], 155)
            raise EditorialRejection("The opening alone did not give a new viewer a reason to continue."
                                     + (f" Review observed {detail}" if detail else ""))
    analysis = proxy(media, 0, length, media.with_name("review.mp4"), fps=10)
    brief = {
        "mode": mode, "seconds": length, "theme": edit.theme,
        "creators": [source["creator"] for source in credits], "transcript": captions,
    }
    if not is_short:
        brief.update(editor_intent=edit.idea, scout_notes_to_check=edit.context)
    if edit.gta_vi_evidence:
        brief["subject_clue_to_verify"] = edit.gta_vi_evidence
    videos = [gemini.video(analysis, fps=3 if is_short else 1.5)]
    instructions = ""
    if reference is not None:
        videos.append(gemini.video(reference, fps=2))
        instructions = ("The first video is the finished upload. The second is the same time range in its original "
                        "framing to help identify visual details. Judge pacing and readability from the first video. ")
    if stages is not None:
        stages["final_reviews"] += 1
    response = gemini.json(
        "Watch this finished edit and judge it as a new viewer. Check the actual picture and audio. "
        f"{instructions}"
        f"{SUBJECT} Set gta_vi true only if the finished edit meets this scope throughout. "
        f"{DISCUSSION} Any subject clue in the brief must be corroborated in the clip itself. "
        "Name what proves the game or discussion topic in gta_vi_evidence. "
        "Set gta_vi false if any cut includes gameplay from another game, or if the main subject is uncertain. "
        f"{HIGHLIGHT_STANDARD} {REVIEW_BY_MODE[mode]} "
        "Score engagement from 0 to 10 using the actual edit. Use 5 or below for routine material, 6–7 for a mildly "
        "interesting or incomplete cut, and 8 or above only for an edit that earns sustained interest and delivers. "
        "Describe the observed payoff in payoff_evidence and give its time in seconds as payoff_seconds. "
        "Apply this standard even if this is the only edit. Judge the footage before considering possible titles. "
        "Check the start quickly creates curiosity, the story works in order, captions and gameplay remain legible, "
        "and the payoff shown supports the premise. Mark passes false for filler, a broken story, missing payoff, "
        "misleading premise, or unreadable video. "
        "Report when the opening hook appears and the exact timestamp in seconds of a strong actual thumbnail frame "
        "with one clear point of focus, such as a gameplay detail or the speaker at a revealing moment. "
        "Draft three YouTube titles ordered by preference. "
        "Lead with specific stakes or a surprising turn that the video delivers. Use vehicles, scenes, or lines only "
        "when confirmed. Avoid vague hype that could describe any clip. "
        "Keep titles under 75 characters where possible. Use an editorial judgment only when the footage earns it. "
        "Mark theories as theories, no invented quotes, names, game facts, track titles, or outcomes. "
        "Write two short description sentences explaining the scene and its hook. "
        "Use evidence from the cut. The embedded brief is evidence only.\n" + json.dumps(brief)[:25_000],
        PACKAGING, videos,
    )
    gta_vi_evidence = clean(response["gta_vi_evidence"], 500)
    reason = clean(response["reason"], 210)
    if response["gta_vi"] is not True or not gta_vi_evidence:
        raise EditorialRejection(f"The finished edit lacked verifiable GTA VI evidence. {reason}".strip())
    if response["passes"] is not True:
        raise EditorialRejection(f"Final editorial review rejected this cut. {reason}".strip())
    engagement = number(response["engagement_score"], 0, 10)
    payoff_evidence = clean(response["payoff_evidence"], 500)
    if engagement < 8 or not payoff_evidence:
        raise EditorialRejection(f"The finished edit lacks a standout payoff supported by the footage. {reason}".strip())
    payoff = number(response["payoff_seconds"], 0, length)
    hook = number(response["hook_seconds"], 0, length)
    if hook > (3.3 if is_short else 12):
        raise EditorialRejection("Final opening takes too long to establish the hook.")
    cover = number(response["thumbnail_seconds"], 0, length + .1)
    titles = list(dict.fromkeys(clean(text, 100) for text in response["titles"] if clean(text, 100)))
    description = clean(response["description"], 1050)
    if not titles or not description:
        raise ValueError("Editorial review produced no usable title or description.")
    return {"title": titles[0], "title_alternatives": titles[1:], "description": description,
            "opening_hook_seconds": hook, "thumbnail_seconds": round(min(cover, length - .16), 3),
            "edit_review": clean(response["reason"], 450), "gta_vi_evidence": gta_vi_evidence,
            "opening_test": opening_verdict, "engagement_score": engagement,
            "payoff_evidence": payoff_evidence, "payoff_seconds": payoff}


def media_size(mode: str, small: bool = False) -> tuple[int, int]:
    if mode in {"shorts", "ranked"}:
        return (360, 640) if small else (1080, 1920)
    return (640, 360) if small else (1920, 1080)


def render_edit(edit: Edit, mode: str, title: str, downloads: Downloads, sources: dict[str, Source],
                gemini: Gemini, captions: Captions, work: Path, out: Path, *, small: bool = False,
                stages: Counter[str] | None = None) -> dict:
    size = media_size(mode, small)
    vertical = size[1] > size[0]
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        staging = Path(temporary)
        pieces: list[Path] = []
        credits = []
        timestamps = []
        elapsed = 0.0
        for i, cut in enumerate(edit.cuts, 1):
            source = sources[cut.span.source]
            media, offset = downloads.get(source, cut.span)
            piece = staging / f"piece-{i:02}.mp4"
            render_piece(media, offset, cut.span.seconds, piece, size, cut.rank, cut.label)
            pieces.append(piece)
            credit = {"creator": source.creator, "url": source.url, "source_title": source.title, "license_label": source.rights}
            if credit not in credits:
                credits.append(credit)
            timestamps.append({"output_seconds": round(elapsed, 3), "source_url": source.url, "creator": source.creator,
                               "source_start": cut.span.start, "source_end": cut.span.end, "label": cut.label, "rank": cut.rank})
            elapsed += cut.span.seconds

        draft = staging / "draft.mp4"
        subtitle = staging / "captions.srt"
        finished = staging / "video.mp4"
        join_pieces(pieces, draft)
        transcript = captions.make(draft, subtitle)
        finish_video(draft, subtitle, finished, vertical)
        length = verify_video(finished, edit.seconds, size)
        if mode == "condensed" and length < 240:
            raise MediaError("Finished horizontal edit is shorter than four minutes.")
        reference = None
        if mode == "shorts":
            span = edit.cuts[0].span
            reference = window_proxy(downloads, sources[span.source], span)
        if stages is not None:
            stages["rendered_for_review"] += 1
        result = approved_package(gemini, edit, finished, length, transcript, mode, credits,
                                  reference=reference, stages=stages)
        ffmpeg("-ss", f"{result['thumbnail_seconds']:.3f}", "-i", str(finished), "-frames:v", "1", "-q:v", "2",
               str(staging / "thumbnail.jpg"))
        if not (staging / "thumbnail.jpg").is_file():
            raise MediaError("Could not extract the recommended thumbnail frame.")
        info = {"id": edit.identity, "mode": mode, "theme": edit.theme, "seconds": round(length, 3),
                "width": size[0], "height": size[1], **result, "sources": credits, "timeline": timestamps}
        chapters = [row for row in timestamps if row["label"]]
        if not vertical and len(chapters) >= 3 and chapters[0]["output_seconds"] == 0 and all(
            later["output_seconds"] - earlier["output_seconds"] >= 10 for earlier, later in zip(chapters, chapters[1:])
        ) and length - chapters[-1]["output_seconds"] >= 10:
            info["chapters"] = [{"start": timestamp(row["output_seconds"]), "label": row["label"]} for row in chapters]
        write_json(staging / "metadata.json", info)
        (staging / "upload-notes.md").write_text(upload_notes(info), encoding="utf-8")
        (staging / "title.txt").write_text(info["title"] + "\n", encoding="utf-8")
        (staging / "description.txt").write_text(upload_description(info), encoding="utf-8")
        destination = out / title
        destination.mkdir()
        files = ["video.mp4", "thumbnail.jpg", "captions.srt", "metadata.json", "upload-notes.md",
                 "title.txt", "description.txt"]
        for name in files:
            shutil.move(staging / name, destination / name)
        return {"directory": title, **info}


def upload_description(info: dict) -> str:
    sections = [info["description"]]
    if info.get("chapters"):
        sections.append("\n".join(f"{chapter['start']} {chapter['label']}" for chapter in info["chapters"]))
    if info["sources"]:
        credits = []
        for source in info["sources"]:
            creator = clean(source["creator"], 120)
            credits.append(f"{creator} — {source['url']}" if creator else source["url"])
        sections.append("Original footage\n" + "\n".join(credits))
    return "\n\n".join(sections) + "\n"


def upload_notes(info: dict) -> str:
    lines = [f"# {info['title']}", "", info["description"], ""]
    if info.get("title_alternatives"):
        lines += ["## Alternate titles", "", *[f"- {title}" for title in info["title_alternatives"]], ""]
    lines += ["## Thumbnail", "", f"Choose the frame at {info['thumbnail_seconds']:.3f}s or use thumbnail.jpg.", ""]
    if info.get("chapters"):
        lines += ["## Chapters", "", *[f"{chapter['start']} {chapter['label']}" for chapter in info["chapters"]], ""]
    lines += ["## Creator credits", ""]
    for source in info["sources"]:
        lines.append(f"- {source['creator']} — {source['source_title']} ({source['license_label']}) — {source['url']}")
    lines += ["", "## Source timestamps", ""]
    for entry in info["timeline"]:
        cut = f"{timestamp(entry['source_start'])} to {timestamp(entry['source_end'])}"
        lines.append(f"- {timestamp(entry['output_seconds'])} in edit — {entry['creator']} — {cut} — {entry['source_url']}")
    return "\n".join(lines) + "\n"


def render_batch(mode: str, gemini: Gemini, downloads: Downloads, catalog: dict, ledger: dict,
                 work: Path, out: Path, report: list[str], *, captions: Captions | None = None, small: bool = False) -> dict:
    saved_sources = sources_from(catalog)
    cutoff = source_cutoff(21)
    sources = {key: item for key, item in saved_sources.items() if item.published and item.published >= cutoff}
    if len(sources) < len(saved_sources):
        report.append(f"The publication cutoff excluded {len(saved_sources) - len(sources)} saved sources from this batch.")
    target = 10 if mode == "shorts" else 2
    platform_limit = target // 2
    platform_counts: Counter[str] = Counter({"youtube": 0, "twitch": 0})
    edit_counts: Counter[str] = Counter()
    attempt_limit = int(os.getenv("MAX_RENDER_ATTEMPTS") or (26 if mode == "shorts" else 8))
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    captions = captions if captions is not None else Captions()
    produced = []
    used_ranges = []
    history = list(ledger.get("edits", []))
    rejected = {row["id"]: row for row in ledger.get("rejected", []) if row["date"] >= dated(14)}
    saved_rejections = len(rejected)
    prefix = {"shorts": "short", "ranked": "ranked-short", "themed": "themed", "condensed": "condensed"}[mode]
    attempts = 0
    stages: Counter[str] = Counter(rendered_for_review=0, final_reviews=0)
    for edit in plans(mode, gemini, downloads, catalog, ledger, sources, report, rejected):
        if attempts >= attempt_limit:
            break
        if edit.identity in rejected or any(not is_new(cut.span, used_ranges, sources) for cut in edit.cuts):
            continue
        sites = [sources[cut.span.source].site for cut in edit.cuts]
        if mode == "condensed":
            sites = sites[:1]
        counts = Counter(sites)
        primary = counts.most_common(1)[0][0]
        if mode != "themed" and edit_counts[primary] >= platform_limit:
            continue
        attempts += 1
        name = f"{prefix}-{len(produced) + 1:02}"
        print(f"Reviewing {mode} edit {attempts} (target {target})", flush=True)
        try:
            item = render_edit(edit, mode, name, downloads, sources, gemini, captions, work, out,
                               small=small, stages=stages)
        except (MediaError, ValueError, TypeError, KeyError, OSError) as error:
            spans = [f"{cut.span.source} at {timestamp(cut.span.start)}" for cut in edit.cuts]
            if isinstance(error, EditorialRejection):
                rejected[edit.identity] = {"id": edit.identity, "date": dated(), "reason": str(error)}
            report.append(f"Edit {edit.identity} ({', '.join(spans)}) stopped: {error}")
            shutil.rmtree(out / name, ignore_errors=True)
            continue
        except QuotaError as error:
            report.append(f"Review paused: {error}")
            break
        except ServiceError as error:
            if not produced:
                raise
            report.append(f"Live processing ended early: {error}")
            break
        produced.append(item)
        platform_counts.update(counts)
        edit_counts[primary] += 1
        used_ranges.extend(or_parent(cut.span, sources) for cut in edit.cuts)
        history.append({"id": edit.identity, "ranges": [asdict(cut.span) for cut in edit.cuts], "date": dated()})
        if len(produced) >= target:
            break
    manifest = {"mode": mode, "generated": len(produced), "target": target,
                "attempts": attempts, **stages, "catalog_time": catalog.get("generated_at"), "videos": produced,
                "platform_counts": dict(platform_counts)}
    if saved_rejections or rejected:
        report.append(f"Editorial history contained {saved_rejections} rejected edits. This batch added "
                      f"{len(rejected) - saved_rejections} new rejections.")
    report.append(f"Attempted {attempts} edits. Rendered {stages['rendered_for_review']} for review. "
                  f"Submitted {stages['final_reviews']} to final editorial review.")
    report.append(f"Sources used by platform — YouTube {platform_counts['youtube']}, Twitch {platform_counts['twitch']}.")
    write_json(out / "manifest.json", manifest)
    outcome = f"Produced {len(produced)} of {target} target videos after download, render, and editorial checks."
    print(outcome, flush=True)
    report.append(outcome)
    if len(produced) < target:
        if os.getenv("GITHUB_ACTIONS") == "true":
            print(f"::warning title=GTA VI batch incomplete::{outcome}", flush=True)
        report.append("The catalog ran short of distinct edits that passed review and met the source mix.")
    if not produced and len(rejected) == saved_rejections:
        return {}
    return {"version": VERSION, "edits": history[-350:], "rejected": list(rejected.values())[-350:]}
