# GTA VI clipper

A scheduled GitHub Actions workflow scouts public GTA VI streams and videos, auditions cuts with Gemini, renders up to ten vertical Shorts, and packages the files for manual upload. This repo also publishes the timestamp catalog and shared engine used by the three other editors.

| Repository | Output |
|---|---|
| [GTA-6-Clipper-shorts](https://github.com/justsomeoneretarded/GTA-6-Clipper-shorts) | Up to 10 single-moment 1080×1920 Shorts, five per platform |
| [GTA-6-Clipper-Ranked-Shorts](https://github.com/justsomeoneretarded/GTA-6-Clipper-Ranked-Shorts) | Up to two 1080×1920 countdowns, each with three moments and an even split across the pair |
| [GTA-6-Clipper-Themed](https://github.com/justsomeoneretarded/GTA-6-Clipper-Themed) | Up to two 1920×1080 episodes, each built from four or six evenly split moments |
| [GTA-6-Clipper-Condensed](https://github.com/justsomeoneretarded/GTA-6-Clipper-Condensed) | Up to two chronological 1920×1080 edits of at least four minutes, one per platform |

## One-time setup

In each repo, go to **Settings → Secrets and variables → Actions** and add the repository secret `GEMINI_API_KEY`. Shorts runs at 8:23 a.m. and 8:23 p.m. Nairobi time. Condensed, Themed, and Ranked Shorts check every 20 minutes for a finished Shorts run that published a catalog. Each starts once per batch, searches for footage for its own format, and adds the exact Shorts catalog as another source of candidates. You can also start any editor manually from the Actions tab without entering URLs or timestamps.

Each editor searches YouTube and Twitch for `GTA 6`, `GTA VI`, `GTA`, `GTA stream`, `GTA game`, and `GTA live`. On Twitch it also reads public clips from the GTA VI category and gets published sections of live replays when available. Scouting and auditions alternate between platforms while both have candidates. Full batches use a 50/50 split, and the manifest reports the actual counts. Discovery accepts sources published within the past seven days, starting November 19, 2026 at midnight Nairobi time. Each editor applies the publication cutoff to its saved catalog too. Unused, dated candidates can stay in the catalog for up to 21 days after publication.

For broader web discovery, add a [Tavily Researcher key](https://app.tavily.com/) as the `TAVILY_API_KEY` secret in the Shorts repo. Each run makes two basic searches. The Shorts repo also accepts optional `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, and `TWITCH_CLIENT_SECRET` secrets for direct platform discovery. The three editing repos only need the Gemini key. Set the `GEMINI_SEARCH_ENABLED` Actions variable to `true` to opt in to Gemini's Google Search with an API project that has billing enabled.

The manual Shorts workflow can include November 18 footage for a batch. The three automatic follow-up runs use that batch's date setting.

The optional `YOUTUBE_COOKIES` Actions secret accepts the exported Netscape-format YouTube `cookies.txt` text or its base64 encoding. The workflow gives yt-dlp a private temporary cookie file for YouTube search, metadata, and media downloads. If FFmpeg cannot fetch a selected range, videos within the full-download time limit can be downloaded with yt-dlp and trimmed locally. An editor that fetches YouTube footage can use its own repository's `YOUTUBE_COOKIES` secret.

The free path defaults to Gemini 3.5 Flash-Lite. The optional Gemini Search path defaults to Gemini 3.8 Flash. Set the Actions variable `GEMINI_MODEL` to choose a specific model. By default the free path uses up to 16 scouting requests and auditions up to 20 moments per run. One scouting request can review up to three short Twitch clips separately. The run report lists the requests and excerpts reviewed for each platform. Set `SCOUT_WINDOWS` and `AUDITION_MOMENTS` to tune batch depth to your [project quota](https://aistudio.google.com/rate-limit). The catalog saves unused candidates for later runs. `MIN_MOMENT_SCORE` sets the quality floor.

If you make the source repo private, supply a `CATALOG_TOKEN` in the consumer repos with read access to the source repo. The catalog contains source links, timestamps and editorial notes on the `clipper-state` branch. Rendered videos are kept in Actions artifacts for 14 days. Artifact access follows the repository's visibility.

## From stream to published artifact

1. Search both platforms for GTA footage and discover creators from the results. Prioritize Twitch clip offsets and YouTube replay peaks when available. Rotate through quieter parts of longer archives.
2. Have Gemini scout likely moments and full scenes. Download promising YouTube windows with `yt-dlp` and cut Twitch replay segments locally. When a YouTube window fails, shorter sources can be downloaded in full and trimmed locally. Move on to reserves when the runner cannot open a source. Condensed previews show an elapsed-time clock to anchor the model's edit points.
3. Audition up to three different openings for the same event. Require a quick hook, complete setup, and a standout payoff visible or audible in the clip. Routine actions with expected results fail the audition. Score hook 25%, payoff 25%, context 20%, specificity 15%, pacing 10%, and phone readability 5%. The export ledger remembers source ranges, visual fingerprints, and final editorial rejections so later runs can skip repeat uploads and try alternate cuts or new sources.
4. Render with FFmpeg and transcribe using faster-whisper. The Shorts layouts center the original footage against a blurred background and burn captions into the video. The ranked editor adds countdown labels. The horizontal editors add short scene labels and include subtitle files.
5. Show Gemini the first 3.3 seconds of each Short on their own to test the hook. The final review checks GTA VI evidence, scores viewer interest, and requires a specific payoff for every format. Rankings require strong entries and a justified winner. Themed videos require distinct scenes that develop the premise. Condensed edits need a complete story with purposeful scenes. Gemini also selects a real thumbnail frame and drafts a title, two alternatives where available, and a description. Download the output from the run's **Artifacts** section.

Each video folder contains `video.mp4`, `title.txt`, `description.txt`, `thumbnail.jpg`, `captions.srt`, `metadata.json`, and `upload-notes.md`. The description is ready to paste and includes creator credits, source links, and chapters where applicable. The artifact also contains a batch manifest and a report with the export count and failures. Small batches are possible when sources are unavailable or fail editorial checks.

## Local verification

Use Python 3.12 and FFmpeg with libass and libx264. The live path also uses Node.js 24 for yt-dlp's YouTube extraction.

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m clipper smoke --output build/fixtures
```

Smoke mode renders synthetic footage through all four editors without API keys. The live jobs run `python -m clipper discover` and `python -m clipper render --mode <mode>`. Each consumer checks out this repo's engine and reads the latest catalog revision on its schedule.
