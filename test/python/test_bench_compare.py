"""Offline tests for the cost-per-task comparison in bench/run.py. No network calls."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
import run as bench  # noqa: E402

OPUS = "bedrock/converse/global.anthropic.claude-opus-5-5"
NEMOTRON = "bedrock/converse/nvidia.nemotron-super-3-120b"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class PricingTests(unittest.TestCase):
    def test_exact_alias_is_priced(self):
        self.assertEqual(bench.price_for("claude-opus-5-5"), (4.0, 20.0))
        self.assertEqual(bench.price_for("nemotron-super-3-120b"), (0.15, 0.65))

    def test_selected_model_header_is_priced_by_bedrock_model_id(self):
        self.assertEqual(bench.price_for(OPUS), (4.0, 20.0))
        self.assertEqual(bench.price_for(NEMOTRON), (0.15, 0.65))
        self.assertEqual(bench.price_for("us.anthropic.claude-opus-5-5"), (4.0, 20.0))

    def test_version_suffixes_match_and_variants_do_not(self):
        self.assertEqual(bench.price_for("anthropic.claude-opus-5-5:0"), (4.0, 20.0))
        self.assertEqual(bench.price_for("anthropic.claude-opus-5-5-v1:0"), (4.0, 20.0))
        self.assertIsNone(bench.price_for("anthropic.claude-opus-5-5-thinking"))
        self.assertIsNone(bench.price_for("anthropic.claude-opus-5-5-20260101-v1:0"))

    def test_routed_group_and_unknown_models_are_unpriced(self):
        self.assertIsNone(bench.price_for("switchyard"))
        self.assertIsNone(bench.price_for("openai/efficient"))

    def test_longest_matching_model_id_wins(self):
        prices = {"small": ("vendor.model", 1.0, 1.0), "large": ("vendor.model-pro", 2.0, 2.0)}
        with mock.patch.dict(bench.PRICES, prices, clear=True):
            self.assertEqual(bench.price_for("bedrock/converse/vendor.model-pro"), (2.0, 2.0))
            self.assertEqual(bench.price_for("bedrock/converse/vendor.model"), (1.0, 1.0))

    def test_canonical_model_still_strips_litellm_prefixes(self):
        self.assertEqual(bench.canonical_model(OPUS), "global.anthropic.claude-opus-5-5")

    def test_cached_input_leaves_turn_unpriced(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "cache_read_input_tokens": 50}
        self.assertIsNone(bench.usage_cost(NEMOTRON, usage))
        self.assertTrue(bench.has_cached_input(usage))
        self.assertFalse(bench.has_cached_input({"prompt_tokens": 1, "completion_tokens": 1}))


def run_result(route, cost, passed, unpriced=(), cached=0, turns=1, error=None, unavailable=0):
    return {"route": route, "estimated_cost": cost, "passed": passed, "error": error,
            "unpriced_models": list(unpriced), "cached_turns": cached, "unavailable_turns": unavailable,
            "turns": turns}


class SummaryTests(unittest.TestCase):
    def test_cost_per_task_and_per_solved(self):
        rows = bench.summary_rows([run_result("switchyard", 0.1, True), run_result("switchyard", 0.3, False)])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["route"], row["runs"], row["solved"]), ("switchyard", 2, 1))
        self.assertAlmostEqual(row["cost_per_task"], 0.2)
        self.assertAlmostEqual(row["cost_per_solved"], 0.4)
        self.assertEqual(row["note"], "")

    def test_routes_keep_first_seen_order(self):
        rows = bench.summary_rows([run_result("switchyard", 0.1, True), run_result("claude-opus-5-5", 0.5, True),
                                   run_result("switchyard", 0.1, True)])
        self.assertEqual([row["route"] for row in rows], ["switchyard", "claude-opus-5-5"])

    def test_nothing_solved_has_no_cost_per_solved(self):
        row = bench.summary_rows([run_result("nemotron-super-3-120b", 0.01, False)])[0]
        self.assertAlmostEqual(row["cost_per_task"], 0.01)
        self.assertIsNone(row["cost_per_solved"])

    def test_one_unpriced_run_makes_the_route_unpriced(self):
        row = bench.summary_rows([run_result("switchyard", 0.1, True),
                                  run_result("switchyard", None, True, unpriced=["vendor.other"])])[0]
        self.assertIsNone(row["cost_per_task"])
        self.assertIsNone(row["cost_per_solved"])
        self.assertIn("add prices to PRICES", row["note"])

    def test_cached_input_is_named_as_the_reason(self):
        row = bench.summary_rows([run_result("switchyard", None, True, cached=2)])[0]
        self.assertIsNone(row["cost_per_task"])
        self.assertIn("cached input", row["note"])
        self.assertNotIn("PRICES", row["note"])

    def test_run_that_failed_before_any_turn_counts_as_zero_with_a_note(self):
        row = bench.summary_rows([run_result("switchyard", 0.0, False, turns=0, error="Request failed: x"),
                                  run_result("switchyard", 0.4, True)])[0]
        self.assertAlmostEqual(row["cost_per_task"], 0.2)
        self.assertAlmostEqual(row["cost_per_solved"], 0.4)
        self.assertEqual(row["errors"], 1)
        self.assertIn("1 run failed before the first turn (counted as $0)", row["note"])

    def test_missing_header_or_usage_is_named_as_the_reason(self):
        row = bench.summary_rows([run_result("switchyard", None, True, unavailable=1)])[0]
        self.assertIsNone(row["cost_per_task"])
        self.assertIn("selected model or usage unavailable", row["note"])
        self.assertNotIn("PRICES", row["note"])

    def test_harness_failure_is_unpriced_and_counted_as_an_error(self):
        row = bench.summary_rows([run_result("switchyard", None, False, turns=0, error="harness: boom")])[0]
        self.assertIsNone(row["cost_per_task"])
        self.assertEqual(row["errors"], 1)
        self.assertIn("harness error", row["note"])

    def test_printed_table_has_the_comparison_columns(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bench.summarize([run_result("switchyard", 0.2, True),
                             run_result("claude-opus-5-5", None, True, unpriced=["x"])])
        text = out.getvalue()
        for column in ("ROUTE", "RUNS", "SOLVED", "ERRORS", "COST/TASK", "COST/SOLVED"):
            self.assertIn(column, text)
        self.assertIn("n/a (add prices to PRICES)", text)
        self.assertIn("0.200000", text)
        self.assertIn("cached", text.lower())


class ModelGroupTests(unittest.TestCase):
    URL = "https://llm-gateway.example.com"

    def test_routed_group_first_then_baselines(self):
        payload = {"data": [{"id": "nemotron-super-3-120b"}, {"id": "switchyard"}, {"id": "claude-opus-5-5"},
                            {"id": "switchyard"}]}
        self.assertEqual(bench.model_groups(lambda: payload, self.URL),
                         ["switchyard", "claude-opus-5-5", "nemotron-super-3-120b"])

    def test_unreachable_gateway_exits_with_clear_message(self):
        def fetch():
            raise urllib.error.URLError("connection refused")
        with self.assertRaises(SystemExit) as raised:
            bench.model_groups(fetch, self.URL)
        self.assertIn(self.URL + "/v1/models", str(raised.exception.code))

    def test_http_error_exits(self):
        def fetch():
            raise urllib.error.HTTPError(self.URL + "/v1/models", 401, "Unauthorized", {}, None)
        with self.assertRaises(SystemExit) as raised:
            bench.model_groups(fetch, self.URL)
        self.assertIn("401", str(raised.exception.code))

    def test_empty_or_malformed_list_exits(self):
        for payload in ({"data": []}, {}, [], {"data": [{"name": "x"}]}, {"data": "switchyard"}):
            with self.assertRaises(SystemExit) as raised:
                bench.model_groups(lambda: payload, self.URL)
            self.assertIn("/v1/models", str(raised.exception.code), payload)

    def test_gateway_lists_groups_through_get_v1_models(self):
        gateway = bench.Gateway(self.URL, "sk-test")
        calls = []

        def request(method, path, session, body=None):
            calls.append((method, path))
            return {"data": [{"id": "claude-opus-5-5"}, {"id": "switchyard"}]}

        gateway.request = request
        self.assertEqual(gateway.model_groups(), ["switchyard", "claude-opus-5-5"])
        self.assertEqual(calls, [("GET", "/v1/models")])


class RouteSelectionTests(unittest.TestCase):
    class Listing:
        def model_groups(self):
            return ["switchyard", "claude-opus-5-5", "nemotron-super-3-120b"]

    def test_default_is_every_group(self):
        args = bench.parse_args([])
        self.assertEqual(bench.resolve_routes(args, self.Listing()),
                         ["switchyard", "claude-opus-5-5", "nemotron-super-3-120b"])

    def test_model_is_an_alias_for_one_route(self):
        args = bench.parse_args(["--model", "claude-opus-5-5"])
        self.assertEqual(bench.resolve_routes(args, self.Listing()), ["claude-opus-5-5"])

    def test_explicit_routes(self):
        args = bench.parse_args(["--routes", "switchyard", "nemotron-super-3-120b", "--repeats", "3"])
        self.assertEqual(bench.resolve_routes(args, self.Listing()), ["switchyard", "nemotron-super-3-120b"])
        self.assertEqual(args.repeats, 3)

    def test_model_and_routes_together_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bench.parse_args(["--model", "switchyard", "--routes", "claude-opus-5-5"])


class StubGateway:
    def __init__(self, payload, model=None):
        self.payload, self.model, self.bodies = payload, model, []

    def request(self, method, path, session, body=None):
        self.bodies.append(body)
        return {**json.loads(json.dumps(self.payload)), "_selected_model": self.model}


class SolveTests(unittest.TestCase):
    def solve(self, gateway, route="switchyard"):
        with tempfile.TemporaryDirectory() as temporary, quiet():
            return bench.solve(gateway, route, "slugify", Path(temporary), max_turns=1)

    def test_empty_choices_body_is_a_run_error(self):
        result = self.solve(StubGateway({"choices": []}, NEMOTRON))
        self.assertFalse(result["passed"])
        self.assertIn("Request failed", result["error"])
        self.assertEqual(result["estimated_cost"], 0.0)

    def test_non_object_choice_is_a_run_error(self):
        result = self.solve(StubGateway({"choices": ["text"]}, NEMOTRON))
        self.assertIn("Request failed", result["error"])

    def test_priced_turn_is_counted(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "done"}}],
                   "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
        result = self.solve(StubGateway(payload, NEMOTRON))
        self.assertAlmostEqual(result["estimated_cost"], 0.15)
        self.assertEqual(result["unpriced_models"], [])
        self.assertEqual(result["cached_turns"], 0)

    def test_unknown_model_and_cached_input_are_recorded(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "done"}}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
        self.assertEqual(self.solve(StubGateway(payload, "vendor.other"))["unpriced_models"], ["vendor.other"])
        payload["usage"]["prompt_tokens_details"] = {"cached_tokens": 5}
        result = self.solve(StubGateway(payload, NEMOTRON))
        self.assertIsNone(result["estimated_cost"])
        self.assertEqual(result["cached_turns"], 1)
        self.assertEqual(result["unpriced_models"], [])

    def test_missing_header_or_usage_is_not_a_missing_price(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "done"}}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
        result = self.solve(StubGateway(payload, None))
        self.assertIsNone(result["estimated_cost"])
        self.assertEqual((result["unavailable_turns"], result["unpriced_models"]), (1, []))
        payload["usage"] = {"prompt_tokens": "10", "completion_tokens": 1}
        result = self.solve(StubGateway(payload, NEMOTRON))
        self.assertEqual((result["unavailable_turns"], result["unpriced_models"]), (1, []))


class SequenceGateway:
    """Answers with each message in turn; records how many requests were made."""

    def __init__(self, *messages):
        self.messages, self.requests = list(messages), 0

    def request(self, method, path, session, body=None):
        self.requests += 1
        return {"choices": [{"message": self.messages.pop(0)}], "_selected_model": NEMOTRON,
                "usage": {"prompt_tokens": 10, "completion_tokens": 1}}


def tool_message(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class ToolCallTests(unittest.TestCase):
    def solve(self, gateway):
        with tempfile.TemporaryDirectory() as temporary, quiet():
            result = bench.solve(gateway, "switchyard", "slugify", Path(temporary), max_turns=3)
            transcript = Path(result["artifacts"]) / "transcript.jsonl"
            result["events"] = [json.loads(line) for line in transcript.read_text().splitlines()]
        return result

    def assert_run_error(self, call):
        gateway = SequenceGateway(tool_message(call), {"role": "assistant", "content": "done"})
        result = self.solve(gateway)
        self.assertIn("Malformed tool call", result["error"] or "", call)
        self.assertFalse(result["passed"])
        self.assertEqual(gateway.requests, 1, "the run stops at the malformed call")

    def test_missing_function_name_or_id_is_a_run_error(self):
        self.assert_run_error({"id": "c1"})
        self.assert_run_error({"id": "c1", "function": "list_files"})
        self.assert_run_error({"id": "c1", "function": {"arguments": "{}"}})
        self.assert_run_error({"function": {"name": "list_files", "arguments": "{}"}})
        self.assert_run_error("list_files")

    def test_invalid_or_non_object_arguments_are_a_run_error(self):
        self.assert_run_error({"id": "c1", "function": {"name": "read_file", "arguments": "{not json"}})
        self.assert_run_error({"id": "c1", "function": {"name": "read_file", "arguments": "[1]"}})

    def test_arguments_already_parsed_into_an_object_are_used(self):
        gateway = SequenceGateway(
            tool_message({"id": "c1", "function": {"name": "read_file", "arguments": {"path": "slug.py"}}}),
            {"role": "assistant", "content": "done"})
        result = self.solve(gateway)
        self.assertIsNone(result["error"])
        self.assertEqual(gateway.requests, 2)
        tools = [event for event in result["events"] if event["event"] == "tool"]
        self.assertEqual(tools[0]["arguments"], {"path": "slug.py"})
        self.assertIn("def slugify", tools[0]["output"])


class RunAllTests(unittest.TestCase):
    def test_unexpected_exception_becomes_a_failed_record_and_the_run_continues(self):
        calls = []

        def explode(gateway, route, task, *args, **kwargs):
            calls.append(route)
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as temporary, quiet(), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(bench, "solve", explode):
            results, summary = bench.run_all(object(), ["switchyard", "claude-opus-5-5"], ["slugify"],
                                             repeats=1, out_dir=Path(temporary))
            records = [json.loads(line) for line in summary.read_text().splitlines()]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bench.summarize(results)
        self.assertEqual(calls, ["switchyard", "claude-opus-5-5"])
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertFalse(record["passed"])
            self.assertTrue(record["error"].startswith("harness: "))
            self.assertIn("boom", record["error"])
            self.assertIsNone(record["estimated_cost"])
        self.assertIn("harness error", out.getvalue())

    def test_one_summary_record_per_run(self):
        class Broken:
            def request(self, method, path, session, body=None):
                raise OSError("offline")

        with tempfile.TemporaryDirectory() as temporary, quiet():
            results, summary = bench.run_all(Broken(), ["switchyard", "claude-opus-5-5"], ["slugify"],
                                             repeats=2, out_dir=Path(temporary), max_turns=1, max_tokens=16)
            records = [json.loads(line) for line in summary.read_text().splitlines()]
            self.assertEqual(summary.parent, Path(temporary))
        self.assertEqual(len(results), 4)
        self.assertEqual([r["route"] for r in records], ["switchyard", "claude-opus-5-5"] * 2)
        self.assertTrue(all("artifacts" in r and "estimated_cost" in r for r in records))


if __name__ == "__main__":
    unittest.main()
