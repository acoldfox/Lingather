import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wordtower_connection", ROOT / "scripts/exporters/wordtower.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
URL = "https://www.youtube.com/watch?v=aDblEBU41nA"
TOKEN = "wtk-" + "a" * 24


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "plugin/skills/wordtower/scripts/request.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text('# --subtitles WORDTOWER_API_KEY .json3 --prepare-only\n')
        (self.script.parent.parent / "config.json").write_text(json.dumps({
            "api_origin": "https://api.wordtower.cn", "gateway_path": "/api/agent/v1/gateway"}))
        self.env = {"WORDTOWER_API_KEY": TOKEN}
        self.preview = {"data": {"preview_id": "preview-1", "title": "Hello", "normalized_chars": 5}}
        self.state_dir = self.root / "states"

    def submit(self, **options):
        return exporter.submit_and_poll(str(self.script), self.preview, "/imports/text", self.env,
            identity="same-input", state_dir=self.state_dir, **options)

    def test_short_links_and_extra_parameters_match_same_video(self):
        for url in ("https://youtu.be/aDblEBU41nA?si=sample", URL + "&t=123", "https://www.youtube.com/shorts/aDblEBU41nA"):
            exporter.validate_youtube_preview({"data": {"preview_id": "p", "media_type": "youtube",
                "source_url": URL, "subtitle_count": 1}}, url)
        for url in ("https://youtube.com.evil.test/watch?v=aDblEBU41nA", "https://user@www.youtube.com/watch?v=aDblEBU41nA", "../bad", "https://youtu.be/aDblEBU41nA/other"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                exporter.canonical_youtube_url(url)

    def test_explicit_plugin_skill_directory_uses_companion_config(self):
        with patch.dict(exporter.os.environ, {"WORDTOWER_SKILL_DIR": str(self.script.parent.parent)}, clear=True):
            self.assertEqual(exporter.find_wordtower_request_script(), str(self.script.resolve()))
            (self.script.parent.parent / "config.json").unlink()
            with self.assertRaises(RuntimeError):
                exporter.find_wordtower_request_script()

    def test_mcp_prepare_strips_keys_and_preserves_word_segments(self):
        source = self.root / "captions.json3"
        source.write_text('{"events":[]}')
        request = {"api_name": "/imports/youtube/preview", "url": URL, "title": "Hello",
            "subtitles": [{"text": "Hello world", "start": 0, "duration": 2,
                "segs": [{"utf8": "Hello ", "tOffsetMs": 0}, {"utf8": "world", "tOffsetMs": 500}]}]}
        output = self.root / "handoff.json"
        with patch.object(exporter, "get_required_request_script", return_value=str(self.script.resolve())), \
             patch.object(exporter, "prepare_env") as auth, patch.object(exporter, "check_capabilities") as caps, \
             patch.dict(exporter.os.environ, {"WORDTOWER_API_KEY": TOKEN, "WORDS_API_KEY": "secret"}), \
             patch.object(exporter, "run_helper", return_value=request) as run:
            result = exporter.export_youtube(URL, source, prepare_only=True, output=output)
            auth.assert_not_called()
            caps.assert_not_called()
            self.assertEqual(result, request)
            self.assertEqual(json.loads(output.read_text()), request)
            self.assertIn("--prepare-only", run.call_args.args[1])
            self.assertNotIn("WORDTOWER_API_KEY", run.call_args.args[2])
            self.assertNotIn("WORDS_API_KEY", run.call_args.args[2])
            with self.assertRaises(FileExistsError):
                exporter.export_youtube(URL, source, prepare_only=True, output=output)

    def test_missing_scope_and_daily_limit_stop_new_import(self):
        for data in ({"scopes": []}, {"scopes": ["imports:text", "imports:read"], "import_usage": {"remaining": 0}}):
            with patch.object(exporter, "run_request_json", return_value={"data": data}), self.assertRaises(RuntimeError):
                exporter.check_capabilities(str(self.script), "text", self.env)
        with patch.object(exporter, "run_request_json", return_value={"data": {
                "scopes": ["imports:text"], "import_usage": {"remaining": 0}}}):
            exporter.check_capabilities(str(self.script), "youtube", self.env, preview_only=True)

    def test_submission_timeout_resumes_original_key(self):
        sent = []
        def uncertain(script, payload, env):
            saved = json.loads(next(self.state_dir.glob("*.json")).read_text())
            self.assertEqual(saved["submit"], payload)
            self.assertNotIn(TOKEN, json.dumps(saved))
            sent.append(payload.copy())
            raise RuntimeError("connection lost after server accepted request")
        with patch.object(exporter, "run_request_json", side_effect=uncertain), self.assertRaises(RuntimeError):
            self.submit()
        state = next(self.state_dir.glob("*.json"))
        def resume(script, payload, env):
            if payload["api_name"] == "/imports/text":
                sent.append(payload.copy())
                return {"data": {"job_id": "job-1"}}
            return {"data": {"status": "succeeded"}}
        with patch.object(exporter, "get_required_request_script", return_value=str(self.script.resolve())), \
             patch.object(exporter, "prepare_env", return_value=self.env), \
             patch.object(exporter, "run_request_json", side_effect=resume):
            exporter.resume_import(state)
        self.assertEqual(sent[0], sent[1])
        with patch.object(exporter, "run_request_json") as request:
            self.submit()
            request.assert_not_called()

    def test_accepted_job_resume_only_queries_status(self):
        with patch.object(exporter, "run_request_json", side_effect=[
                {"data": {"job_id": "job-2"}}, {"data": {"status": "running"}}]):
            self.submit(max_wait=0)
        state = next(self.state_dir.glob("*.json"))
        with patch.object(exporter, "get_required_request_script", return_value=str(self.script.resolve())), \
             patch.object(exporter, "prepare_env", return_value=self.env), \
             patch.object(exporter, "run_request_json", return_value={"data": {"status": "succeeded"}}) as request:
            exporter.resume_import(state)
            self.assertEqual(request.call_args.args[1], {"api_name": "/imports/status", "job_id": "job-2"})
            self.assertEqual(request.call_count, 1)

    def test_another_key_or_destination_cannot_resume(self):
        with patch.object(exporter, "run_request_json", side_effect=RuntimeError("timeout")), self.assertRaises(RuntimeError):
            self.submit()
        path = next(self.state_dir.glob("*.json"))
        for env in ({"WORDTOWER_API_KEY": "wtk-" + "b" * 24}, self.env):
            if env is self.env:
                (self.script.parent.parent / "config.json").write_text(json.dumps({
                    "api_origin": "https://different.example", "gateway_path": "/api/agent/v1/gateway"}))
            with patch.object(exporter, "get_required_request_script", return_value=str(self.script.resolve())), \
                 patch.object(exporter, "prepare_env", return_value=env), \
                 patch.object(exporter, "run_request_json") as request, self.assertRaises(RuntimeError):
                exporter.resume_import(path)
            request.assert_not_called()

    def test_terminal_failure_never_automatically_creates_another_task(self):
        with patch.object(exporter, "run_request_json", side_effect=[
                {"data": {"job_id": "job-3"}}, {"data": {"status": "failed"}}]), self.assertRaises(RuntimeError):
            self.submit()
        with patch.object(exporter, "run_request_json") as request, self.assertRaises(RuntimeError):
            self.submit()
        request.assert_not_called()
        with patch.object(exporter, "run_request_json", side_effect=[
                {"data": {"job_id": "job-4"}}, {"data": {"status": "succeeded"}}]):
            self.submit(new_import=True)
        self.assertEqual(len(list(self.state_dir.glob("*.json"))), 2)

    def test_recovery_lock_blocks_parallel_submission(self):
        self.state_dir.mkdir()
        state = self.state_dir / "active.json"
        with exporter.state_lock(state):
            with self.assertRaises(RuntimeError):
                with exporter.state_lock(state):
                    self.fail("must not acquire a second lock")
        self.assertFalse(state.with_suffix(".json.lock").exists())

    def test_changed_input_stops_before_submit(self):
        source = self.root / "text.txt"
        source.write_text("Original")
        def preview(*args):
            source.write_text("Changed")
            return self.preview
        with patch.object(exporter, "get_required_request_script", return_value=str(self.script.resolve())), \
             patch.object(exporter, "prepare_env", return_value=self.env), patch.object(exporter, "check_capabilities"), \
             patch.object(exporter, "run_helper", side_effect=preview), patch.object(exporter, "submit_and_poll") as submit, \
             self.assertRaises(RuntimeError):
            exporter.export_text(source)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
