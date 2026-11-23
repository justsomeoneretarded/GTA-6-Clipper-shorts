from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .core import VERSION, other_game_ranges, read_json, recent_rejections, write_json
from .discovery import date_twitch_clips, discover
from .editorial import build_catalog, merge_catalogs
from .gemini import Gemini, ServiceError
from .media import Downloads, youtube_cookie_file
from .production import MODES, render_batch, sources_from


def write_report(path: Path, mode: str, report: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# GTA VI {mode} run", "", *[f"- {line}" for line in report]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n\n")


def main() -> int:
    parser = argparse.ArgumentParser(prog="clipper")
    parser.add_argument("stage", choices=("discover", "render", "smoke"))
    parser.add_argument("--mode", choices=MODES, default="shorts")
    parser.add_argument("--state", type=Path, default=Path("build/state"))
    parser.add_argument("--work", type=Path, default=Path("build/work"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.stage == "smoke":
        from .smoke import smoke

        smoke(args.output.resolve())
        return 0

    state, work, output = (path.resolve() for path in (args.state, args.work, args.output))
    report: list[str] = []
    youtube_cookies = os.environ.pop("YOUTUBE_COOKIES", "")
    try:
        gemini = Gemini()
        with youtube_cookie_file(youtube_cookies, report) as cookie_file:
            downloads = Downloads(work / "media", youtube_cookies=cookie_file)
            catalog = read_json(state / "catalog.json", {"version": VERSION, "moments": [], "sequences": [], "sources": {}})
            ledger = read_json(state / "ledger.json", {})
            shared_ledger = read_json(state / "shared-ledger.json", {})
            blocked = other_game_ranges(ledger) + other_game_ranges(shared_ledger)
            if blocked:
                report.append(f"Excluded {len(blocked)} previously identified ranges showing other games.")
            if args.stage == "discover":
                sources_from(catalog)
                shared = read_json(state / "shared-catalog.json") if args.mode != "shorts" else None
                if shared:
                    sources_from(shared)
                    catalog = merge_catalogs(catalog, shared)
                    report.append(f"Shared catalog added to discovery with {len(shared['moments'])} moments "
                                  f"and {len(shared['sequences'])} longer sequences.")
                new_sources = discover(gemini, downloads, report)
                old_clips = {key: source for key, source in sources_from(catalog).items()
                             if source.platform == "twitch-clip" and not source.recorded and key not in new_sources}
                date_twitch_clips(old_clips, report)
                catalog["sources"].update({key: asdict(source) for key, source in old_clips.items()})
                catalog = build_catalog(gemini, downloads, new_sources, catalog, report, mode=args.mode,
                                        rejected=recent_rejections(ledger), blocked=blocked)
                write_json(state / "catalog.json", catalog)
            else:
                pending = render_batch(args.mode, gemini, downloads, catalog, ledger, work, output, report, blocked=blocked)
                if pending:
                    write_json(state / "next-ledger.json", pending)
    except (ServiceError, ValueError, OSError) as error:
        report.append(f"Workflow stopped: {error}")
        print(f"::error::{error}", file=sys.stderr)
        return 1
    finally:
        write_report(output / f"{args.stage}-report.md", args.mode, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
