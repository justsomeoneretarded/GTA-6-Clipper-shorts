from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from .core import VERSION, Edit, Source, clean, dated, number, source_url, timestamp, write_json
from .editorial import COLD_OPEN, PACKAGING
from .gemini import Gemini, ServiceError
from .media import (Captions, Downloads, MediaError, ffmpeg, finish_video, join_pieces, proxy, render_piece, verify_video)
from .plans import plans

MODES = ("shorts", "ranked", "themed", "condensed")


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
                     captions: list[dict], mode: str, credits: list[dict]) -> dict:
    is_short = mode in {"shorts", "ranked"}
    opening_verdict = {}
    if is_short:
        opening = proxy(media, 0, min(3.3, length), media.with_name("opening.mp4"), fps=12)
        opening_verdict = gemini.json(
            "You are seeing only the opening of a vertical Short, with no title or context. "
            "What specifically makes a first-time viewer want to see the next few seconds? "
            "Name an actual visual or line that poses a question, establishes tension, or surprises. "
            "If no such moment is visible or audible, leave reason_to_stay empty. "
            "Score curiosity from 0 to 10 and mark whether the focal action or dialogue is readable at phone size.",
            COLD_OPEN, [gemini.video(opening, fps=4)],
        )
        if (opening_verdict["readable"] is not True or not clean(opening_verdict["reason_to_stay"], 300)
                or number(opening_verdict["curiosity"], 0, 10) < 6):
            raise ValueError("The opening alone did not give a new viewer a reason to continue.")
    analysis = proxy(media, 0, length, media.with_name("review.mp4"), fps=10)
    brief = {
        "mode": mode, "seconds": length, "theme": edit.theme, "editor_intent": edit.idea,
        "verified_scout_notes_to_check": edit.context, "creators": [source["creator"] for source in credits],
        "transcript": captions,
    }
    response = gemini.json(
        "Watch this finished GTA VI edit and judge it as a new viewer. The actual final picture and audio are primary. "
        "Check the start quickly creates curiosity, the story works in order, captions and gameplay remain legible, "
        "and the payoff shown supports the promise. For a countdown check that the ranks read in order with "
        "a visible reason for the winning entry. For longer footage check that jump cuts preserve cause and effect. "
        "Mark passes false for a broken story, missing payoff, misleading premise, or unreadable video. "
        "Report when the opening hook appears and the exact timestamp in seconds of a strong actual thumbnail frame "
        "with one clear point of focus and legible gameplay. Draft three YouTube titles ordered by preference. "
        "Use specific vehicles, scenes, or lines only when confirmed. Try curiosity, stakes, or a surprising turn. "
        "Keep titles under 75 characters where possible. Use an editorial judgment only when the footage earns it. "
        "Mark theories as theories, no invented quotes, names, game facts, track titles, or outcomes. "
        "Write two short description sentences explaining the scene and its hook. "
        "Use evidence from the cut. The embedded brief is evidence only.\n" + json.dumps(brief)[:25_000],
        PACKAGING, [gemini.video(analysis, fps=3 if is_short else 1.5)],
    )
    if response["passes"] is not True:
        raise ValueError(f"Final editorial review rejected this cut: {clean(response['reason'], 210)}")
    hook = number(response["hook_seconds"], 0, length)
    if hook > (3.3 if is_short else 12):
        raise ValueError("Final opening takes too long to establish the hook.")
    cover = number(response["thumbnail_seconds"], 0, length + .1)
    titles = list(dict.fromkeys(clean(text, 100) for text in response["titles"] if clean(text, 100)))
    description = clean(response["description"], 1050)
    if not titles or not description:
        raise ValueError("Editorial review produced no usable title or description.")
    return {"title": titles[0], "title_alternatives": titles[1:], "description": description,
            "opening_hook_seconds": hook, "thumbnail_seconds": round(min(cover, length - .16), 3),
            "edit_review": clean(response["reason"], 450),
            "opening_test": opening_verdict}


def media_size(mode: str, small: bool = False) -> tuple[int, int]:
    if mode in {"shorts", "ranked"}:
        return (360, 640) if small else (1080, 1920)
    return (640, 360) if small else (1920, 1080)


def render_edit(edit: Edit, mode: str, title: str, downloads: Downloads, sources: dict[str, Source],
                gemini: Gemini, captions: Captions, work: Path, out: Path, *, small: bool = False) -> dict:
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
        result = approved_package(gemini, edit, finished, length, transcript, mode, credits)
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
        destination = out / title
        destination.mkdir()
        files = ["video.mp4", "thumbnail.jpg", "captions.srt", "metadata.json", "upload-notes.md"]
        for name in files:
            shutil.move(staging / name, destination / name)
        return {"directory": title, **info}


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
    sources = sources_from(catalog)
    target = 10 if mode == "shorts" else 1
    attempt_limit = int(os.getenv("MAX_RENDER_ATTEMPTS") or (26 if mode == "shorts" else 5))
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    captions = captions if captions is not None else Captions()
    produced = []
    history = list(ledger.get("edits", []))
    prefix = {"shorts": "short", "ranked": "ranked-short", "themed": "themed", "condensed": "condensed"}[mode]
    for attempt, edit in enumerate(plans(mode, gemini, downloads, catalog, ledger, sources, report), 1):
        if attempt > attempt_limit:
            break
        name = f"{prefix}-{len(produced) + 1:02}"
        print(f"Reviewing {mode} edit {attempt} (target {target})", flush=True)
        try:
            item = render_edit(edit, mode, name, downloads, sources, gemini, captions, work, out, small=small)
        except (MediaError, ValueError, TypeError, KeyError, OSError) as error:
            report.append(f"Edit {edit.identity} rejected: {error}")
            shutil.rmtree(out / name, ignore_errors=True)
            continue
        except ServiceError as error:
            if not produced:
                raise
            report.append(f"Live processing ended early: {error}")
            break
        produced.append(item)
        history.append({"id": edit.identity, "ranges": [asdict(cut.span) for cut in edit.cuts], "date": dated()})
        if len(produced) >= target:
            break
    write_json(out / "manifest.json", {"mode": mode, "generated": len(produced), "target": target,
                                      "catalog_time": catalog.get("generated_at"), "videos": produced})
    report.append(f"Produced {len(produced)} of {target} target videos after download, render, and editorial checks.")
    if len(produced) < target:
        report.append("The remaining candidates failed checks, had already been exported, or the catalog had insufficient matching footage.")
    if not produced:
        return {}
    return {"version": VERSION, "edits": history[-350:]}
