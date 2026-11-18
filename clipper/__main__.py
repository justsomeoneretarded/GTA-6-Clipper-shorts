from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .core import VERSION, read_json, write_json
from .discovery import discover
from .editorial import build_catalog
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
            if args.stage == "discover":
                sources_from(catalog)
                new_sources = discover(gemini, downloads, report)
                catalog = build_catalog(gemini, downloads, new_sources, catalog, report)
                write_json(state / "catalog.json", catalog)
            else:
                ledger = read_json(state / "ledger.json", {})
                pending = render_batch(args.mode, gemini, downloads, catalog, ledger, work, output, report)
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
