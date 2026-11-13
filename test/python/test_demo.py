import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
import run as demo

# A test module that records where it ran and what it could see.
PROBE = """import json
import os
from pathlib import Path
import unittest


class Probe(unittest.TestCase):
    def test_environment(self):
        Path("written-by-tests.txt").write_text("x")
        print("PROBE " + json.dumps({"cwd": os.getcwd(), "home": os.environ.get("HOME"),
                                     "keys": sorted(os.environ)}))
"""


class DemoTests(unittest.TestCase):
    def test_usage_cost_requires_concrete_model_and_complete_usage(self):
        self.assertIsNone(demo.usage_cost("switchyard", {"prompt_tokens": 10, "completion_tokens": 5}))
        self.assertIsNone(demo.usage_cost("nvidia.nemotron-super-3-120b", {}))
        self.assertAlmostEqual(demo.usage_cost("bedrock/converse/nvidia.nemotron-super-3-120b",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}), 0.8)

    def test_cached_usage_is_not_reported_at_uncached_rates(self):
        self.assertIsNone(demo.usage_cost("global.anthropic.claude-opus-5-5", {
            "prompt_tokens": 100, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 90}}))

    def test_tool_paths_and_test_files_are_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertIn("outside", demo.run_tool(root, "write_file", {"path": "../escape.py", "content": "bad"}))
            self.assertIn("read-only", demo.run_tool(root, "write_file", {"path": "test_example.py", "content": "bad"}))

    def test_failed_request_preserves_task_and_records_failure(self):
        class BrokenGateway:
            def request(self, method, path, session, body):
                raise OSError("offline test failure")
        with tempfile.TemporaryDirectory() as temporary:
            with contextlib.redirect_stdout(io.StringIO()):
                result = demo.solve(BrokenGateway(), "switchyard", "slugify", Path(temporary), max_turns=1)
            artifact = Path(result["artifacts"])
            self.assertFalse(result["passed"])
            self.assertEqual(result["estimated_cost"], 0.0)  # failed before any turn: nothing to bill
            self.assertIn("offline test failure", result["error"])
            self.assertTrue((artifact / "original/slug.py").is_file())
            self.assertTrue((artifact / "workspace/slug.py").is_file())
            records = [json.loads(line) for line in (artifact / "transcript.jsonl").read_text().splitlines()]
            self.assertEqual(records[-1]["event"], "result")

    def test_multiturn_defaults_to_every_group_and_has_no_judge_accounting(self):
        import multiturn
        self.assertIsNone(multiturn.parse_args([]).routes)
        self.assertFalse(hasattr(multiturn.Client, "session_stats"))
        self.assertTrue(multiturn.over_budget([{"model": None, "cost": None}], 1.0))


class OpenUrlTests(unittest.TestCase):
    def test_file_and_custom_schemes_never_reach_urlopen(self):
        with mock.patch.object(demo.urllib.request, "urlopen") as urlopen:
            for url in ["file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi", "localhost:4000/v1"]:
                with self.assertRaisesRegex(ValueError, "Only http and https"):
                    demo.open_url(url, timeout=1)
            with self.assertRaises(ValueError):
                demo.open_url(urllib.request.Request("file:///etc/passwd"), timeout=1)
        urlopen.assert_not_called()

    def test_the_error_names_only_the_scheme(self):
        with self.assertRaises(ValueError) as raised:
            demo.open_url("file:///home/user/.aws/credentials", timeout=1)
        self.assertIn("'file'", str(raised.exception))
        self.assertNotIn(".aws", str(raised.exception))

    def test_http_and_https_urls_and_requests_are_opened(self):
        request = urllib.request.Request("https://llm-gateway.example.com/v1/models")
        with mock.patch.object(demo.urllib.request, "urlopen", return_value="response") as urlopen:
            self.assertEqual(demo.open_url("http://127.0.0.1:4000/health/liveliness", timeout=5), "response")
            self.assertEqual(demo.open_url(request, timeout=30), "response")
        self.assertEqual(urlopen.call_args_list, [mock.call("http://127.0.0.1:4000/health/liveliness", timeout=5),
                                                  mock.call(request, timeout=30)])


class ToolEnvironmentTests(unittest.TestCase):
    ENVIRONMENT = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TMPDIR": "/tmp/bench", "HOME": "/home/reader",
                   "AWS_SECRET_ACCESS_KEY": "example", "AWS_PROFILE": "default", "LITELLM_MASTER_KEY": "sk-local"}

    def test_sandbox_env_keeps_only_path_lang_tmpdir_and_the_given_home(self):
        with mock.patch.dict(os.environ, self.ENVIRONMENT, clear=True):
            self.assertEqual(demo.sandbox_env(home="/tmp/bench/work"),
                             {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TMPDIR": "/tmp/bench", "HOME": "/tmp/bench/work"})
            self.assertEqual(demo.sandbox_env(), {"PATH": "/usr/bin", "LANG": "C.UTF-8", "TMPDIR": "/tmp/bench"})

    def test_run_tests_runs_a_copy_outside_the_workspace_with_home_set_and_no_credentials(self):
        secrets = {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_PROFILE": "default", "LITELLM_MASTER_KEY": "sk-local"}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, secrets):
            root = Path(temporary).resolve()
            (root / "test_probe.py").write_text(PROBE)
            output = demo.run_tool(root, "run_tests", {})
            self.assertFalse((root / "written-by-tests.txt").exists(), "the tests ran in the workspace")
        self.assertTrue(output.rstrip().endswith("OK"), output)
        seen = json.loads(next(line for line in output.splitlines() if line.startswith("PROBE "))[6:])
        ran_in = Path(seen["cwd"]).resolve()
        self.assertNotEqual(ran_in, root)
        self.assertNotIn(ROOT, [ran_in, *ran_in.parents], "the tests ran inside the repository")
        self.assertEqual(Path(seen["home"]).resolve(), ran_in)
        self.assertEqual([key for key in seen["keys"] if key.startswith(("AWS_", "LITELLM_"))], [])


if __name__ == "__main__":
    unittest.main()
