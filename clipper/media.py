from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from .core import Range, Source, digest, source_url, timestamp


class MediaError(RuntimeError):
    pass


@contextmanager
def youtube_cookie_file(secret: str, report: list[str]):
    """Keep a scoped Netscape cookie jar outside the media and artifact directories."""
    if not secret.strip():
        yield None
        return

    headers = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
    contents = secret.lstrip("\ufeff").strip()
    if not contents.startswith(headers):
        try:
            contents = base64.b64decode("".join(contents.split()), validate=True).decode("utf-8-sig").strip()
        except (binascii.Error, UnicodeError):
            contents = ""
    rows = []
    if contents.startswith(headers):
        for line in contents.splitlines()[1:]:
            fields = line.split("\t")
            domain = fields[0].removeprefix("#HttpOnly_").lstrip(".").casefold()
            if len(fields) == 7 and (domain == "youtube.com" or domain.endswith(".youtube.com")):
                rows.append(line)
    if not rows:
        report.append("The YouTube secret needs a Netscape-format cookies.txt export containing YouTube cookies.")
        yield None
        return

    with tempfile.TemporaryDirectory(prefix="gta6-youtube-") as directory:
        cookie_file = Path(directory) / "cookies.txt"
        fd = os.open(cookie_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write("\n".join([headers[0], *rows, ""]))
        report.append("YouTube cookies were enabled for yt-dlp.")
        yield cookie_file


def youtube_error(stderr: str) -> str:
    detail = stderr.casefold()
    for fragment, reason in (
        ("sign in to confirm", "YouTube asked to sign in to confirm this request."),
        ("cookies are no longer valid", "YouTube rejected the saved session cookies."),
        ("requested format is not available", "YouTube did not offer the requested video format."),
        ("no video formats found", "YouTube did not return any downloadable video formats."),
        ("video is not available", "This YouTube video is not available to the runner."),
        ("ffmpeg is not installed", "FFmpeg is required for this YouTube request."),
        ("timed out", "The authenticated YouTube request timed out."),
    ):
        if fragment in detail:
            return reason
    status = re.search(r"\b(?:http(?: error)?|server returned)\s+([45]\d\d)\b", detail)
    if status and "ffmpeg" in detail:
        return f"FFmpeg received HTTP {status.group(1)} while reading YouTube media."
    if status:
        return f"YouTube returned HTTP {status.group(1)} for the authenticated request."
    if "ffmpeg" in detail:
        return "FFmpeg could not read the YouTube media."
    return "The authenticated YouTube request failed."


def run(args: list[str], *, timeout: int = 240, cwd: Path | None = None,
        summarize_error: Callable[[str], str] | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError(f"{args[0]} timed out after {timeout}s") from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr or error.stdout or ""
        if summarize_error:
            raise MediaError(summarize_error(detail)) from None
        lines = detail.strip().splitlines()
        raise MediaError(f"{args[0]} failed: {' '.join(lines[-2:])[:330]}") from None
    except OSError as error:
        raise MediaError(f"Could not run {args[0]}: {error.strerror}") from None
    return result.stdout


def ffmpeg(*args: str, cwd: Path | None = None, timeout: int = 240) -> None:
    run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-filter_threads", "2",
         "-filter_complex_threads", "2", "-y", *args], cwd=cwd, timeout=timeout)


def probe(path: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=25))


def video_duration(path: Path) -> float:
    info = probe(path)
    if not any(stream["codec_type"] == "video" for stream in info["streams"]):
        raise MediaError("Downloaded file contains no video.")
    return float(info["format"].get("duration") or 0)


class Downloads:
    def __init__(self, directory: Path, fixtures: dict[str, Path] | None = None,
                 *, youtube_cookies: Path | None = None):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.windows: dict[str, list[tuple[Range, Path]]] = {}
        self.fixtures = fixtures or {}
        self.unreachable: dict[str, str] = {}
        self.youtube_cookies = youtube_cookies
        self.full_limit = int(os.getenv("FULL_DOWNLOAD_MAX_SECONDS") or 3600)
        self.format = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]/b"

    def ytdlp(self, url: str, extra: list[str], *, timeout: int) -> str:
        source = source_url(url)
        authenticated = self.youtube_cookies and (url.startswith("ytsearch") or source and source.platform == "youtube")
        args = ["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress", "--no-warnings",
                "--socket-timeout", "20", "--retries", "2", "--js-runtimes", "node"]
        if authenticated:
            args.extend(["--cookies", str(self.youtube_cookies)])
        return run([*args, *extra, url], timeout=timeout, summarize_error=youtube_error if authenticated else None)

    def metadata(self, source: Source) -> None:
        result = json.loads(self.ytdlp(source.url, ["--dump-single-json", "--skip-download"], timeout=65))
        if result.get("is_live") or result.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise MediaError("The broadcast archive is still being prepared.")
        source.duration = float(result.get("duration") or 0)
        source.title = source.title or str(result.get("title") or "")
        source.creator = source.creator or str(result.get("uploader") or "Unknown creator")
        source.views = source.views or int(result.get("view_count") or 0)
        for peak in sorted(result.get("heatmap") or [], key=lambda row: row.get("value", 0), reverse=True):
            start = float(peak.get("start_time") or 0)
            if 0 <= start < source.duration and all(abs(start - other) >= 105 for other in source.anchors):
                source.anchors.append(start)
            if len(source.anchors) >= 3:
                break

    def _fetch(self, source: Source, span: Range, whole: bool) -> Path:
        target = self.directory / f"{digest('whole', source.key) if whole else span.identity}.mp4"
        if target.exists():
            return target
        with tempfile.TemporaryDirectory(dir=self.directory) as work:
            temporary = Path(work)
            native_youtube = whole and source.platform == "youtube" and self.youtube_cookies
            media_format = "b[height<=1080][ext=mp4]/b[ext=mp4]/b" if native_youtube else self.format
            options = ["-f", media_format, "--merge-output-format", "mp4", "--remux-video", "mp4",
                       "-o", str(temporary / "video.%(ext)s")]
            if native_youtube:
                options += ["--downloader", "native"]
            if not whole:
                options += ["--download-sections", f"*{span.start:.3f}-{span.end:.3f}", "--force-keyframes-at-cuts"]
            timeout = 75 if source.platform == "twitch-clip" else (420 if whole else 180)
            try:
                self.ytdlp(source.url, options, timeout=timeout)
            except MediaError:
                if source.platform != "twitch-clip" or not shutil.which("twitch-dl"):
                    raise
                run(["twitch-dl", "download", "--quality", "source", "--overwrite",
                     "--output", str(temporary / "video.mp4"), source.url], timeout=90)
            file = temporary / "video.mp4"
            if not file.is_file():
                raise MediaError("Downloader did not return an MP4.")
            if not whole and abs(video_duration(file) - span.seconds) > min(2, span.seconds * .15):
                raise MediaError("Section download length differs from the requested range.")
            file.replace(target)
        return target

    def get(self, source: Source, span: Range) -> tuple[Path, float]:
        """Return a readable media path and the requested start relative to that file."""
        if source.key != span.source or span.start < 0 or span.end > source.duration + .15 or span.seconds <= 0:
            raise MediaError("Requested edit range falls outside its source.")
        if source.key in self.fixtures:
            return self.fixtures[source.key], span.start
        full = self.directory / (digest("whole", source.key) + ".mp4")
        if full.exists():
            return full, span.start
        for coverage, file in self.windows.get(source.key, []):
            if coverage.start <= span.start and coverage.end >= span.end and file.exists():
                return file, span.start - coverage.start
        if source.key in self.unreachable:
            raise MediaError(self.unreachable[source.key])
        if source.platform == "twitch-clip":
            return self._fetch(source, span, whole=True), span.start
        try:
            file = self._fetch(source, span, whole=False)
        except MediaError as section_failure:
            failure = str(section_failure).casefold()
            blocked = any(reason in failure for reason in ("403", "sign in", "bot", "not available", "private"))
            ffmpeg_youtube = source.platform == "youtube" and self.youtube_cookies and "ffmpeg" in failure
            if source.duration > self.full_limit or (blocked and not ffmpeg_youtube):
                self.unreachable[source.key] = str(section_failure)
                raise
            try:
                return self._fetch(source, span, whole=True), span.start
            except MediaError as full_failure:
                self.unreachable[source.key] = str(full_failure)
                raise
        self.windows.setdefault(source.key, []).append((span, file))
        return file, 0


def proxy(source_path: Path, start: float, length: float, destination: Path, *, fps: int = 8) -> Path:
    if destination.exists():
        return destination
    bitrate = max(68, min(500, int(87_000 / length) - 44))
    width = 640 if length <= 300 else 480
    ffmpeg("-ss", f"{start:.3f}", "-i", str(source_path), "-t", f"{length:.3f}",
           "-vf", f"scale='min({width},iw)':-2,fps={fps}", "-c:v", "libx264", "-threads", "2",
           "-preset", "veryfast", "-b:v", f"{bitrate}k", "-maxrate", f"{bitrate}k",
           "-bufsize", f"{bitrate * 2}k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "40k",
           "-ac", "1", "-movflags", "+faststart", str(destination))
    return destination


def window_proxy(downloads: Downloads, source: Source, span: Range, *, fps: int = 8) -> Path:
    path = downloads.directory / f"proxy-{span.identity}-{fps}.mp4"
    if path.exists():
        return path
    media, offset = downloads.get(source, span)
    return proxy(media, offset, span.seconds, path, fps=fps)


def fingerprint(path: Path, start: float, length: float) -> str:
    """Three low-resolution difference hashes for spotting duplicate scenes."""
    hashes = []
    for part in (.18, .48, .8):
        result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start + length * part:.3f}",
                                 "-i", str(path), "-frames:v", "1", "-vf", "scale=9:8,format=gray",
                                 "-threads", "1", "-f", "rawvideo", "-"], capture_output=True, timeout=20)
        pixels = result.stdout
        if result.returncode or len(pixels) != 72:
            return ""
        value = 0
        for y in range(8):
            for x in range(8):
                value = (value << 1) | (pixels[y * 9 + x] > pixels[y * 9 + x + 1])
        hashes.append(f"{value:016x}")
    return ".".join(hashes)


def render_piece(media: Path, offset: float, length: float, destination: Path,
                 size: tuple[int, int], rank: int | None, label: str) -> None:
    width, height = size
    inputs = ["-ss", f"{offset:.3f}", "-i", str(media)]
    has_audio = any(stream["codec_type"] == "audio" for stream in probe(media)["streams"])
    audio = ["-map", "0:a:0", "-af", "apad"] if has_audio else ["-map", "1:a:0"]
    if not has_audio:
        inputs.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
    if height > width:
        framing = (f"[0:v]split=2[back][front];[back]scale={width}:{height}:force_original_aspect_ratio=increase,"
                   f"crop={width}:{height},boxblur=20:1[blur];[front]scale={width}:{height}:"
                   "force_original_aspect_ratio=decrease:force_divisible_by=2[fit];[blur][fit]overlay=(W-w)/2:(H-h)/2")
    else:
        framing = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease:"
                   f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    framing += ",setsar=1,fps=30"
    if rank or label:
        textfile = destination.with_suffix(".label.srt")
        label = label.translate(str.maketrans("", "", r"\{}<>"))
        wrapping = "\n".join(textwrap.wrap((f"#{rank}  " if rank else "") + label[:45], 28))
        end = length if rank else min(length, 3.2)
        textfile.write_text(f"1\n00:00:00,000 --> {srt_time(end)}\n{wrapping}\n\n", encoding="utf-8")
        framing += (f",subtitles={textfile.name}:force_style='FontName=DejaVu Sans,FontSize=17,"
                    "Bold=1,BorderStyle=3,Outline=5,BackColour=&H78000000,Alignment=8,MarginV=40'")
    framing += "[video]"
    ffmpeg(*inputs, "-t", f"{length:.3f}", "-filter_complex", framing, "-map", "[video]", *audio,
           "-c:v", "libx264", "-threads", "3", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(destination),
           cwd=destination.parent, timeout=480)


def join_pieces(pieces: list[Path], destination: Path) -> None:
    concat = destination.with_suffix(".txt")
    concat.write_text("".join(f"file '{piece.name}'\n" for piece in pieces), encoding="utf-8")
    ffmpeg("-f", "concat", "-safe", "1", "-i", concat.name, "-c", "copy",
           "-movflags", "+faststart", str(destination), cwd=destination.parent)


def srt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    whole, remainder = divmod(millis, 1000)
    return f"{timestamp(whole)},{remainder:03d}"


class Captions:
    def __init__(self):
        self.model = None

    def make(self, media: Path, srt: Path) -> list[dict]:
        import numpy as np

        if self.model is None:
            from faster_whisper import WhisperModel

            self.model = WhisperModel(os.getenv("WHISPER_MODEL") or "base", device="cpu",
                                      compute_type="int8", cpu_threads=3)
        with tempfile.TemporaryDirectory(dir=media.parent) as work:
            samples = Path(work) / "audio.f32"
            ffmpeg("-i", str(media), "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "f32le", str(samples))
            audio = np.fromfile(samples, dtype="<f4")
        segments, _ = self.model.transcribe(audio, vad_filter=True, word_timestamps=True,
                                            beam_size=3, condition_on_previous_text=False)
        captions = []
        for segment in segments:
            words = []
            for word in segment.words or []:
                line = "".join(item.word for item in words) + word.word
                if words and (len(line.strip()) > 42 or word.end - words[0].start > 2.8 or word.start - words[-1].end > .6):
                    captions.append({"start": words[0].start, "end": words[-1].end, "text": "".join(item.word for item in words).strip()})
                    words = []
                words.append(word)
            if words:
                captions.append({"start": words[0].start, "end": words[-1].end, "text": "".join(item.word for item in words).strip()})
        srt.write_text("".join(
            f"{i}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['text']}\n\n"
            for i, cue in enumerate(captions, 1)
        ), encoding="utf-8")
        return captions


def finish_video(source: Path, captions: Path, destination: Path, vertical: bool) -> None:
    if vertical and captions.stat().st_size:
        video_filter = f"subtitles={captions.name}:force_style='FontName=DejaVu Sans,FontSize=17,Outline=2,MarginV=46,Alignment=2'"
        ffmpeg("-i", str(source), "-vf", video_filter, "-c:v", "libx264", "-threads", "3",
               "-preset", "veryfast", "-crf", "19", "-c:a", "copy", "-movflags", "+faststart",
               str(destination), cwd=source.parent)
    else:
        shutil.copyfile(source, destination)


def verify_video(path: Path, length: float, size: tuple[int, int]) -> float:
    info = probe(path)
    video = next((stream for stream in info["streams"] if stream["codec_type"] == "video"), {})
    if (video.get("width"), video.get("height")) != size or video.get("codec_name") != "h264":
        raise MediaError("Rendered video has unexpected dimensions or encoding.")
    if not any(stream["codec_type"] == "audio" and stream["codec_name"] == "aac" for stream in info["streams"]):
        raise MediaError("Rendered video is missing its AAC audio stream.")
    actual = float(info["format"]["duration"])
    if abs(actual - length) > 1.25:
        raise MediaError(f"Render has {actual:.2f}s of media, expected {length:.2f}s.")
    ffmpeg("-ss", f"{max(0, actual - 1):.3f}", "-i", str(path), "-t", "0.4", "-f", "null", "-")
    return actual
