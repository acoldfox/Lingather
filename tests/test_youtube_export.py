import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"

spec = importlib.util.spec_from_file_location("exporter", SCRIPTS_DIR / "exporters/wordtower.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

yt_spec = importlib.util.spec_from_file_location("fetch_youtube", SCRIPTS_DIR / "fetch_youtube.py")
fetch_youtube = importlib.util.module_from_spec(yt_spec)
yt_spec.loader.exec_module(fetch_youtube)

URL = "https://www.youtube.com/watch?v=pTdvsfIMGes"


class YouTubeExportTests(unittest.TestCase):
    def test_rejects_plain_text_preview_before_submit(self):
        with tempfile.NamedTemporaryFile(suffix=".srt") as subtitles:
            with patch.object(exporter, "check_capabilities"), \
                 patch.object(exporter, "prepare_env", return_value={"configured": "yes"}), \
                 patch.object(exporter, "find_wordtower_request_script", return_value="request.py"), \
                 patch.object(exporter.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout=json.dumps({"data": {"preview_id": "plain"}}))), \
                 patch.object(exporter, "submit_and_poll") as submit:
                with self.assertRaises(RuntimeError):
                    exporter.export_youtube(URL, subtitles.name)
                submit.assert_not_called()

    def test_timed_preview_preserves_youtube_command(self):
        preview = {"data": {"preview_id": "timed", "media_type": "youtube",
                            "source_url": URL, "subtitle_count": 260}}
        with tempfile.NamedTemporaryFile(suffix=".srt") as subtitles:
            with patch.object(exporter, "check_capabilities"), \
                 patch.object(exporter, "prepare_env", return_value={"configured": "yes"}), \
                 patch.object(exporter, "find_wordtower_request_script", return_value="request.py"), \
                 patch.object(exporter.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout=json.dumps(preview))) as run, \
                 patch.object(exporter, "submit_and_poll") as submit:
                exporter.export_youtube(URL, subtitles.name)
                command = run.call_args.args[0]
                self.assertEqual(command[2:6], ["--youtube", URL, "--subtitles", str(Path(subtitles.name).resolve())])
                self.assertEqual(submit.call_args.args, ("request.py", preview, "/imports/text", {"configured": "yes"}))
                self.assertFalse(submit.call_args.kwargs["preview_only"])

    def test_json3_preview_preserves_word_timings(self):
        preview = {"data": {"preview_id": "timed-w", "media_type": "youtube",
                            "source_url": URL, "subtitle_count": 169, "word_timing_count": 1589}}
        with tempfile.NamedTemporaryFile(suffix=".json3") as subtitles, \
             tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8") as req_script:
            req_script.write("# supports .json3\n")
            req_script.flush()
            with patch.object(exporter, "check_capabilities"), \
                 patch.object(exporter, "prepare_env", return_value={"configured": "yes"}), \
                 patch.object(exporter, "find_wordtower_request_script", return_value=req_script.name), \
                 patch.object(exporter.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout=json.dumps(preview))) as run, \
                 patch.object(exporter, "submit_and_poll") as submit:
                exporter.export_youtube(URL, subtitles.name, title="Doctor Shift")
                command = run.call_args.args[0]
                self.assertEqual(
                    command[2:8],
                    ["--youtube", URL, "--subtitles", str(Path(subtitles.name).resolve()), "--title", "Doctor Shift"],
                )
                self.assertEqual(submit.call_args.args, (req_script.name, preview, "/imports/text", {"configured": "yes"}))
                self.assertFalse(submit.call_args.kwargs["preview_only"])

    def test_json3_rejects_outdated_request_script(self):
        with tempfile.NamedTemporaryFile(suffix=".json3") as subtitles, \
             tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8") as req_script:
            req_script.write("# legacy 0.7.0 without json3\n")
            req_script.flush()
            with patch.object(exporter, "check_capabilities"), \
                 patch.object(exporter, "prepare_env", return_value={"configured": "yes"}), \
                 patch.object(exporter, "find_wordtower_request_script", return_value=req_script.name):
                with self.assertRaises(RuntimeError):
                    exporter.export_youtube(URL, subtitles.name)

    def test_rejects_empty_or_wrong_video_timeline(self):
        for count, url in [(0, URL), (True, URL), (260, URL + "x")]:
            with self.assertRaises(RuntimeError):
                exporter.validate_youtube_preview({"data": {"preview_id": "p",
                    "media_type": "youtube", "source_url": url, "subtitle_count": count}}, URL)

    def test_text_path_cannot_import_srt_or_json3(self):
        for suffix in (".srt", ".vtt", ".json3"):
            with tempfile.NamedTemporaryFile(suffix=suffix) as subtitles:
                with patch.object(exporter, "check_capabilities"), \
                 patch.object(exporter, "prepare_env", return_value={"configured": "yes"}), \
                     patch.object(exporter, "find_wordtower_request_script", return_value="request.py"), \
                     patch.object(exporter.subprocess, "run") as run:
                    with self.assertRaises(ValueError):
                        exporter.export_text(subtitles.name)
                    run.assert_not_called()


class FetchYouTubeSubtitleTests(unittest.TestCase):
    def test_canonical_video_url_variants(self):
        for raw in (
            "arrTr-ymaZs",
            "https://youtu.be/arrTr-ymaZs?si=abc",
            "https://www.youtube.com/shorts/arrTr-ymaZs",
            "https://www.youtube.com/watch?v=arrTr-ymaZs&t=12",
        ):
            url, vid = fetch_youtube.canonical_video_url(raw)
            self.assertEqual(vid, "arrTr-ymaZs")
            self.assertEqual(url, "https://www.youtube.com/watch?v=arrTr-ymaZs")

    def test_invalid_single_video_sources_are_rejected(self):
        for value in ("https://youtube.com.evil.test/watch?v=arrTr-ymaZs", "https://youtu.be/arrTr-ymaZs/other", "../unsafe"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fetch_youtube.canonical_video_url(value)

    def test_fetch_command_forwards_mcp_handoff_without_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "captions.json3"
            source.write_text('{"events":[]}')
            target = Path(temporary) / "handoff.json"
            with patch.object(fetch_youtube.sys, "argv", ["fetch_youtube.py", "wordtower", URL,
                    "--prepare-only", "--handoff-output", str(target)]), \
                 patch.object(fetch_youtube, "download_subtitles_with_meta", return_value=(source, {"video_url": URL})), \
                 patch.object(fetch_youtube.subprocess, "run") as run:
                fetch_youtube.main()
            command = run.call_args.args[0]
            self.assertIn("--prepare-only", command)
            self.assertEqual(command[-2:], ["--output", str(target)])
            self.assertIn("--subtitles", command)

    def test_track_selection_prefers_non_translated_native_json3(self):
        metadata = {
            "subtitles": {},
            "automatic_captions": {
                "en": [
                    {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=ar&tlang=en&fmt=json3"},
                    {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=vtt"},
                    {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=json3"},
                ]
            },
        }
        candidates = fetch_youtube._iter_track_candidates(
            metadata, ["en", "en-orig"], ["json3", "vtt", "srt"]
        )
        self.assertEqual(
            candidates[0],
            ("en", "json3", "https://www.youtube.com/api/timedtext?lang=en&fmt=json3"),
        )
        self.assertEqual(
            candidates[-1],
            ("en", "json3", "https://www.youtube.com/api/timedtext?lang=ar&tlang=en&fmt=json3"),
        )

    def test_download_subtitles_with_meta_extracts_title_and_json3(self):
        metadata = {
            "title": "Reality of a 13-Hour Night Shift as a Doctor",
            "subtitles": {},
            "automatic_captions": {
                "en": [
                    {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=json3"},
                ]
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_download(url, dest_path, fmt, proxy=None):
                dest_path.write_text('{"events":[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"Hi"}]}]}', encoding="utf-8")
                return True

            with patch.object(fetch_youtube, "get_ytdlp_command", return_value=["yt-dlp"]), \
                 patch.object(fetch_youtube, "_fetch_video_metadata", return_value=metadata), \
                 patch.object(fetch_youtube, "_download_track_url", side_effect=fake_download):
                sub_path, meta = fetch_youtube.download_subtitles_with_meta(
                    "https://youtu.be/arrTr-ymaZs",
                    output_dir=tmpdir,
                    sub_format="json3,vtt,srt",
                )
            self.assertTrue(sub_path.name.endswith(".json3"))
            self.assertEqual(meta["title"], "Reality of a 13-Hour Night Shift as a Doctor")
            self.assertEqual(meta["video_url"], "https://www.youtube.com/watch?v=arrTr-ymaZs")


if __name__ == "__main__":
    unittest.main()
