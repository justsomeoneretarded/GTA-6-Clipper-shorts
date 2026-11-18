import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipper.media import Downloads, MediaError, youtube_cookie_file


EXPORT = (
    "# Netscape HTTP Cookie File\r\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tfixture-cookie\r\n"
    ".google.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-other-session\r\n"
)


class YouTubeCookieTests(unittest.TestCase):
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

    @patch("clipper.media.run", side_effect=MediaError("HTTP 403 with fixture-cookie"))
    def test_cookie_error_hides_provider_details(self, run):
        with tempfile.TemporaryDirectory() as folder, youtube_cookie_file(EXPORT, []) as path:
            downloads = Downloads(Path(folder), youtube_cookies=path)
            with self.assertRaises(MediaError) as raised:
                downloads.ytdlp("https://www.youtube.com/watch?v=TestGTA0001", [], timeout=20)
            self.assertEqual(str(raised.exception), "YouTube denied the authenticated request (HTTP 403).")
