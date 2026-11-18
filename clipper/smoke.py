"""Offline media smoke test that exercises each production editor with a fixture model."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from .core import VERSION, Moment, Range, Sequence, Source, read_json, source_cutoff
from .editorial import COLD_OPEN, GROUP_EDIT, PACKAGING, SEQUENCE_EDIT
from .media import Downloads, ffmpeg, probe
from .plans import COMPACT
from .production import MODES, render_batch


class FixtureModel:
    def video(self, file: Path, fps: float = 2) -> dict:
        assert file.stat().st_size > 1024
        return {"fixture": file.name}

    def json(self, prompt: str, schema: dict, media: list | None = None) -> dict:
        if schema is COLD_OPEN:
            return {"reason_to_stay": "Fixture opening", "readable": True, "curiosity": 9}
        if schema is PACKAGING:
            return {"gta_vi": True, "gta_vi_evidence": "Synthetic editorial fixture",
                    "passes": True, "reason": "Synthetic fixture", "hook_seconds": 0.5,
                    "engagement_score": 9, "payoff_evidence": "Synthetic payoff", "payoff_seconds": 10,
                    "thumbnail_seconds": 2, "titles": ["Fixture video", "Alternate fixture"],
                    "description": "A generated test video."}
        if schema is COMPACT:
            return {"cuts": []}
        if schema is GROUP_EDIT:
            ranked = "#3" in prompt
            youtube_count = 1 if ranked and "Use 1 YouTube" in prompt else 2
            briefs = json.loads(prompt.rsplit("\n", 1)[-1])
            youtube = [entry for entry in briefs if entry["platform"] == "youtube"]
            twitch = [entry for entry in briefs if entry["platform"] == "twitch"]
            chosen = youtube[:youtube_count] + twitch[:(3 if ranked else 4) - youtube_count]
            return {"idea": "Fixture sequence", "theme": "chase", "context": "Testing grouped edits.",
                    "entries": [{"id": item["id"], "label": f"Moment {i}"} for i, item in enumerate(chosen, 1)]}
        if schema is SEQUENCE_EDIT:
            return {"idea": "Fixture chronology", "story": "Preserve chronological order through a synthetic edit.",
                    "cuts": [{"start": a, "end": b, "beat": f"Beat {i}"} for i, (a, b) in
                             enumerate([(0, 62), (82, 146), (169, 239), (249, 310)], 1)]}
        raise AssertionError("Unexpected fixture model request")


class FixtureCaptions:
    def make(self, path: Path, srt: Path) -> list[dict]:
        srt.write_text("1\n00:00:00,100 --> 00:00:02,000\nFixture caption\n\n", encoding="utf-8")
        return [{"start": .1, "end": 2, "text": "Fixture caption"}]


def smoke(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        input_path = work / "fixture.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12", "-f", "lavfi", "-i", "sine=frequency=520",
               "-t", "310", "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-c:a", "aac", str(input_path))
        item = Source("youtube:TestGTA0001", "https://www.youtube.com/watch?v=TestGTA0001", "youtube",
                      "Synthetic source", "Fixture creator", 310, published=source_cutoff(0))
        twitch = Source("twitch-vod:1234567890", "https://www.twitch.tv/videos/1234567890", "twitch-vod",
                        "Synthetic Twitch replay", "Fixture broadcaster", 310, published=source_cutoff(0))
        moments = [Moment(Range(twitch.key if i % 2 == 0 else item.key, start, start + 13), "chase", f"Opening {i}",
                          "Setup", "Payoff", "Synthetic evidence", 92 - i)
                   for i, start in enumerate(range(12, 289, 24), 1)]
        sequences = [Sequence(Range(source.key, 0, 310), "chase", "Synthetic sequence", 90) for source in (item, twitch)]
        catalog = {"version": VERSION, "moments": [obj.to_dict() for obj in moments],
                   "sequences": [obj.to_dict() for obj in sequences],
                   "sources": {source.key: asdict(source) for source in (item, twitch)}}
        model = FixtureModel()
        downloads = Downloads(work / "cache", {item.key: input_path, twitch.key: input_path})
        for mode in MODES:
            out = output / mode
            report: list[str] = []
            ledger = render_batch(mode, model, downloads, catalog, {}, work, out, report,
                                  captions=FixtureCaptions(), small=True)
            expected = 10 if mode == "shorts" else 2
            assert len(ledger.get("edits", [])) == expected, (mode, report)
            per_site = {"shorts": 5, "ranked": 3, "themed": 4, "condensed": 1}[mode]
            assert read_json(out / "manifest.json")["platform_counts"] == {"youtube": per_site, "twitch": per_site}
            for result in out.glob("*/video.mp4"):
                info = probe(result)
                if mode == "condensed":
                    assert float(info["format"]["duration"]) >= 240
                assert (result.parent / "thumbnail.jpg").is_file()
                assert (result.parent / "upload-notes.md").is_file()
                metadata = read_json(result.parent / "metadata.json")
                assert (result.parent / "title.txt").read_text(encoding="utf-8").strip() == metadata["title"]
                description = (result.parent / "description.txt").read_text(encoding="utf-8")
                assert description.startswith(metadata["description"] + "\n")
                for source in metadata["sources"]:
                    assert f"{source['creator']} — {source['url']}" in description
                if mode == "ranked":
                    assert [cut["rank"] for cut in metadata["timeline"]] == [3, 2, 1]
            print(f"{mode}: rendered and verified {expected} fixture video(s)", flush=True)
