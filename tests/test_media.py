import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper.core import Range, source_url
from clipper.media import Downloads, MediaError, publication_date, youtube_cookie_file


EXPORT = (
    "# Netscape HTTP Cookie File\r\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tfixture-cookie\r\n"
    ".google.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-other-session\r\n"
)


class YouTubeCookieTests(unittest.TestCase):
    def test_yt_dlp_publication_date_supports_uploads_and_streams(self):
        self.assertEqual(publication_date({"upload_date": "20261117"}), "2026-11-17T00:00:00Z")
        self.assertEqual(publication_date({"release_timestamp": 1700000000, "upload_date": "20190605"}),
                         "2023-11-14T22:13:20Z")
        self.assertEqual(publication_date({"upload_date": "20261301"}), "")

    def test_cookie_export_is_scoped_and_removed_after_use(self):
        for value in (EXPORT, base64.b64encode(EXPORT.encode()).decode()):
            with self.subTest(encoded=value != EXPORT):
                with youtube_cookie_file(value, []) as path:
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertIn("#HttpOnly_.youtube.com", path.read_text())
                    self.assertNotIn("fixture-other-session", path.read_text())
                self.assertFalse(path.exists())

    def test_invalid_cookie_export_keeps_public_discovery_available(self):
        report = []
        with youtube_cookie_file("an unsupported export", report) as path:
            self.assertIsNone(path)
        self.assertIn("Netscape-format", report[0])

    @patch("clipper.media.run", return_value="{}")
    def test_only_youtube_commands_receive_cookie_path(self, run):
        with tempfile.TemporaryDirectory() as folder, youtube_cookie_file(EXPORT, []) as path:
            downloads = Downloads(Path(folder), youtube_cookies=path)
            downloads.ytdlp("ytsearch1:GTA VI", [], timeout=20)
            self.assertIn(str(path), run.call_args.args[0])
            downloads.ytdlp("https://www.youtube.com/watch?v=TestGTA0001", [], timeout=20)
            self.assertIn("--cookies", run.call_args.args[0])
            downloads.ytdlp("https://clips.twitch.tv/FixtureClip", [], timeout=20)
            self.assertNotIn("--cookies", run.call_args.args[0])

    @patch("clipper.media.subprocess.run", side_effect=subprocess.CalledProcessError(
        1, ["yt-dlp"], stderr="Server returned 403 Forbidden with fixture-cookie\nFFmpeg failed\n"))
    def test_cookie_error_hides_provider_details(self, run):
        with tempfile.TemporaryDirectory() as folder, youtube_cookie_file(EXPORT, []) as path:
            downloads = Downloads(Path(folder), youtube_cookies=path)
            with self.assertRaises(MediaError) as raised:
                downloads.ytdlp("https://www.youtube.com/watch?v=TestGTA0001", [], timeout=20)
            self.assertEqual(str(raised.exception), "FFmpeg received HTTP 403 while reading YouTube media.")

    def test_authenticated_youtube_section_failure_retries_full_download(self):
        with tempfile.TemporaryDirectory() as folder, youtube_cookie_file(EXPORT, []) as path:
            downloads = Downloads(Path(folder), youtube_cookies=path)
            source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
            source.duration = 300
            span = Range(source.key, 10, 35)
            file = Path(folder) / "fixture.mp4"
            with patch.object(downloads, "_fetch", side_effect=[
                MediaError("FFmpeg received HTTP 403 while reading YouTube media."), file,
            ]) as fetch:
                self.assertEqual(downloads.get(source, span), (file, 10))
            self.assertTrue(fetch.call_args.kwargs["whole"])


class TwitchReplayTests(unittest.TestCase):
    @patch("clipper.media.video_duration", return_value=13)
    @patch("clipper.media.run")
    def test_recording_replay_uses_a_bounded_download_and_keeps_fractional_offsets(self, run, duration):
        def write_media(args, **kwargs):
            Path(args[args.index("--output") + 1]).touch()
            return ""

        run.side_effect = write_media
        with tempfile.TemporaryDirectory() as folder:
            downloads = Downloads(Path(folder))
            source = source_url("https://www.twitch.tv/videos/1234567890")
            source.recording = True
            source.duration = 100
            file, offset = downloads.get(source, Range(source.key, 10.25, 22.75))
            command = run.call_args.args[0]
            self.assertEqual(command[0], "twitch-dl")
            self.assertEqual(command[command.index("--start") + 1], "00:00:10")
            self.assertEqual(command[command.index("--end") + 1], "00:00:23")
            self.assertEqual(offset, 0.25)
            self.assertEqual(downloads.get(source, Range(source.key, 12, 21)), (file, 2))
            self.assertEqual(run.call_count, 1)

    def test_failed_recording_section_does_not_download_the_entire_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            downloads = Downloads(Path(folder))
            source = source_url("https://www.twitch.tv/videos/1234567890")
            source.recording = True
            source.duration = 120
            span = Range(source.key, 40, 70)
            with patch.object(downloads, "_fetch", side_effect=MediaError("Unavailable section")) as fetch:
                with self.assertRaises(MediaError):
                    downloads.get(source, span)
            fetch.assert_called_once_with(source, span, whole=False)

    def test_bad_replay_section_does_not_block_an_earlier_range(self):
        with tempfile.TemporaryDirectory() as folder:
            downloads = Downloads(Path(folder))
            source = source_url("https://www.twitch.tv/videos/1234567890")
            source.recording = True
            source.duration = 700
            file = Path(folder) / "earlier.mp4"
            with patch.object(downloads, "_fetch", side_effect=[MediaError("Section length mismatch"), file]) as fetch:
                with self.assertRaises(MediaError):
                    downloads.get(source, Range(source.key, 500, 650))
                self.assertEqual(downloads.get(source, Range(source.key, 80, 130)), (file, 0))
                self.assertEqual(fetch.call_count, 2)
