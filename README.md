# GTA VI clipper

A scheduled GitHub Actions workflow scouts public GTA VI streams and videos, auditions cuts with Gemini, renders up to ten vertical Shorts, and packages the files for manual upload. This repo also publishes the timestamp catalog and shared engine used by the three other editors.

| Repository | Output |
|---|---|
| [GTA-6-Clipper-shorts](https://github.com/justsomeoneretarded/GTA-6-Clipper-shorts) | Up to 10 single-moment 1080×1920 Shorts per run |
| [GTA-6-Clipper-Ranked-Shorts](https://github.com/justsomeoneretarded/GTA-6-Clipper-Ranked-Shorts) | One 1080×1920 countdown with three ranked moments |
| [GTA-6-Clipper-Themed](https://github.com/justsomeoneretarded/GTA-6-Clipper-Themed) | One 1920×1080 episode that develops a theme across 4–6 moments |
| [GTA-6-Clipper-Condensed](https://github.com/justsomeoneretarded/GTA-6-Clipper-Condensed) | One chronological 1920×1080 edit of a gameplay sequence, at least four minutes |

## One-time setup

In each repo, go to **Settings → Secrets and variables → Actions** and add the repository secret `GEMINI_API_KEY`. Run **Actions → Produce GTA VI Shorts → Run workflow** first to populate the catalog. The four workflows run on daily schedules. You can run them again from the Actions tab without entering URLs or timestamps.

The Shorts discovery workflow checks both YouTube and Twitch. The default path checks public Twitch listings and tries YouTube search when other discovery providers supply zero YouTube links. Gemini reviews footage and writes the upload package. YouTube may challenge hosted media fetches even when a video appears in search.

For broader web discovery, add a [Tavily Researcher key](https://app.tavily.com/) as the `TAVILY_API_KEY` secret in the Shorts repo. Each run makes two basic searches. The Shorts repo also accepts optional `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, and `TWITCH_CLIENT_SECRET` secrets for direct platform discovery. The three editing repos only need the Gemini key. Set the `GEMINI_SEARCH_ENABLED` Actions variable to `true` to opt in to Gemini's Google Search with an API project that has billing enabled.

The optional `YOUTUBE_COOKIES` Actions secret accepts the exported Netscape-format YouTube `cookies.txt` text or its base64 encoding. The workflow gives yt-dlp a private temporary cookie file for YouTube search, metadata, and media downloads. If FFmpeg cannot fetch a selected range, videos within the full-download time limit can be downloaded with yt-dlp and trimmed locally. An editor that fetches YouTube footage can use its own repository's `YOUTUBE_COOKIES` secret.

The free path defaults to Gemini 3.5 Flash-Lite. The optional Gemini Search path defaults to Gemini 3.8 Flash. Set the Actions variable `GEMINI_MODEL` to choose a specific model. The default free setup starts with five scouting windows and four auditions per run. Set `SCOUT_WINDOWS` and `AUDITION_MOMENTS` to tune batch depth to your [project quota](https://aistudio.google.com/rate-limit). The catalog saves unused candidates for later runs. `MIN_MOMENT_SCORE` sets the quality floor.

If you make the source repo private, supply a `CATALOG_TOKEN` in the consumer repos with read access to the source repo. The catalog contains source links, timestamps and editorial notes on the `clipper-state` branch. Rendered videos are kept in Actions artifacts for 14 days. Artifact access follows the repository's visibility.

## From stream to published artifact

1. Search across established GTA creators and new channels. Prioritize Twitch clip offsets and YouTube replay peaks when available. Rotate through quieter parts of longer archives.
2. Have Gemini scout likely moments and full scenes. Download the promising windows with `yt-dlp`. For a range-specific failure, try downloading a short-enough source once and cut locally. For public Twitch clips, try `twitch-dl` if needed. Move on to reserves when the runner cannot open a source.
3. Audition up to three different in-order openings for the same event. Require visible or audible evidence, a quick hook, complete setup and a payoff. Score hook 25%, payoff 25%, context 20%, specificity 15%, pacing 10%, and phone readability 5%. Compare evidence from the strongest contenders and reduce repetition using source ranges, Twitch parent timestamps, visual fingerprints, creator diversity, and the export ledger.
4. Render with FFmpeg and transcribe using faster-whisper. The Shorts layouts center the original footage against a blurred background and burn captions into the video. The ranked editor adds countdown labels. The horizontal editors add short scene labels and include subtitle files.
5. Show Gemini the first 3.3 seconds of each Short on their own to test the hook. Then have Gemini review the rendered edit, select a real thumbnail frame, and draft a title, two alternatives where available, and a description. Editors reject cuts with a broken story, an unsupported payoff or a slow opening. Download the output from the run's **Artifacts** section.

Each video folder contains `video.mp4`, `thumbnail.jpg`, `captions.srt`, `metadata.json`, and `upload-notes.md` with the suggested cover timestamp, creator credits, source times, and applicable chapter markers. The artifact also contains a batch manifest and a report with the export count and failures. Small batches are possible when sources are unavailable or fail editorial checks. Source credit and any platform license label are recorded for your review before posting.

## Local verification

Use Python 3.12 and FFmpeg with libass and libx264. The live path also uses Node.js 24 for yt-dlp's YouTube extraction.

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m clipper smoke --output build/fixtures
```

Smoke mode renders synthetic footage through all four editors without API keys. The live jobs run `python -m clipper discover` and `python -m clipper render --mode <mode>`. Each consumer checks out this repo's engine and reads the latest catalog revision on its schedule.
