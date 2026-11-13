"""Offline tests for bench/multiturn.py. No network calls; coding tasks are not run."""
from concurrent.futures import Future
import contextlib
import inspect
import io
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bench"))
import multiturn  # noqa: E402

NEMOTRON = "bedrock/converse/nvidia.nemotron-super-3-120b"


def response(payload=None, model=NEMOTRON, error=None, status=200):
    return {"http_status": status, "attempts": 1, "retry_wait_ms": 0, "retry_statuses": [], "latency_ms": 1,
            "router_headers": {"x-litellm-model-name": model} if model else {}, "payload": payload,
            "error": error}


def reply(content="Canberra", prompt=1000, completion=100, **usage):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, **usage}}


class StubClient:
    def __init__(self, *responses):
        self.responses = list(responses)

    def chat(self, route, session, messages, max_tokens, tools=None):
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def args(*argv):
    return multiturn.parse_args(["--tasks", "--scripts", "backend", *argv])


class ArgumentTests(unittest.TestCase):
    def test_default_routes_come_from_the_gateway(self):
        self.assertIsNone(multiturn.parse_args([]).routes)

    def test_zero_conversation_cap_means_no_cap(self):
        self.assertIsNone(args("--max-conv-cost", "0").max_conv_cost)
        self.assertEqual(args().max_conv_cost, 1.0)

    def test_negative_conversation_cap_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            args("--max-conv-cost", "-1")

    def test_tasks_must_be_task_directories(self):
        self.assertEqual(multiturn.parse_args(["--tasks"]).tasks, [])
        self.assertEqual(multiturn.parse_args(["--tasks", "slugify", "roman"]).tasks, ["slugify", "roman"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            multiturn.parse_args(["--tasks", "__pycache__"])

    def test_unused_cost_import_and_client_param_are_gone(self):
        self.assertFalse(hasattr(multiturn, "cost"))
        self.assertNotIn("client", inspect.signature(multiturn.conversation_record).parameters)


class ModelGroupTests(unittest.TestCase):
    def test_client_lists_groups_routed_first(self):
        client = multiturn.Client("https://llm-gateway.example.com", "sk-test")
        client._request = lambda method, path, session, body=None: (
            200, {}, {"data": [{"id": "nemotron-super-3-120b"}, {"id": "switchyard"}]})
        self.assertEqual(client.model_groups(), ["switchyard", "nemotron-super-3-120b"])

    def test_client_exits_when_the_list_fails(self):
        client = multiturn.Client("https://llm-gateway.example.com", "sk-test")

        def refuse(method, path, session, body=None):
            raise urllib.error.URLError("connection refused")

        client._request = refuse
        with self.assertRaises(SystemExit) as raised:
            client.model_groups()
        self.assertIn("/v1/models", str(raised.exception.code))


class PricingTests(unittest.TestCase):
    BASE = {"route": "switchyard"}

    def test_priced_turn(self):
        rec = multiturn.turn_record(self.BASE, 1, response(reply(prompt=1_000_000, completion=0)), 2)
        self.assertAlmostEqual(rec["cost"], 0.15)

    def test_unknown_model_and_cached_input_are_unpriced(self):
        self.assertIsNone(multiturn.turn_record(self.BASE, 1, response(reply(), model="vendor.other"), 2)["cost"])
        cached = response(reply(prompt_tokens_details={"cached_tokens": 10}))
        rec = multiturn.turn_record(self.BASE, 1, cached, 2)
        self.assertIsNone(rec["cost"])
        self.assertEqual(multiturn.unpriced_models([rec]), [NEMOTRON + " (cached input)"])

    def test_reply_without_header_or_usage_is_unpriced(self):
        rec = multiturn.turn_record(self.BASE, 1, response({"choices": []}, model=None), 2)
        self.assertIsNone(rec["cost"])
        self.assertEqual(multiturn.unpriced_models([rec]), ["unknown (model or usage unavailable)"])

    def test_failed_request_without_usage_costs_nothing(self):
        rec = multiturn.turn_record(self.BASE, 1, response(None, model=None, error="HTTP 400", status=400), 2)
        self.assertEqual(rec["cost"], 0.0)

    def test_over_budget(self):
        priced = [{"model": NEMOTRON, "cost": 0.4}, {"model": NEMOTRON, "cost": 0.7}]
        unpriced = [{"model": "vendor.other", "cost": None}]
        self.assertTrue(multiturn.over_budget(priced, 1.0))
        self.assertFalse(multiturn.over_budget(priced, 2.0))
        self.assertTrue(multiturn.over_budget(unpriced, 2.0))
        self.assertFalse(multiturn.over_budget(unpriced, None))
        self.assertFalse(multiturn.over_budget(unpriced, None, None))
        self.assertTrue(multiturn.over_budget(unpriced, None, 5.0), "a spend budget alone needs prices too")
        self.assertFalse(multiturn.over_budget(priced, None, 0.5), "spend is checked between conversations")
        self.assertIn("cannot be enforced", multiturn.budget_error(unpriced, 2.0))
        self.assertIn("cap 1.0 reached", multiturn.budget_error(priced, 1.0))


class StopReasonTests(unittest.TestCase):
    UNPRICED = {"unpriced_models": ["vendor.other"]}

    def test_unpriced_spend_stops_the_run_when_a_budget_is_set(self):
        self.assertIn("vendor.other", multiturn.stop_reason(self.UNPRICED, 1.0, None))
        self.assertIsNotNone(multiturn.stop_reason(self.UNPRICED, None, 5.0))

    def test_no_budget_keeps_going(self):
        self.assertIsNone(multiturn.stop_reason(self.UNPRICED, None, None))

    def test_priced_and_harness_records_keep_going(self):
        self.assertIsNone(multiturn.stop_reason({"unpriced_models": []}, 1.0, 5.0))
        self.assertIsNone(multiturn.stop_reason({"error": "harness: boom", "cost": None}, 1.0, 5.0))


class ConversationTests(unittest.TestCase):
    def test_empty_choices_200_body_is_a_turn_error(self):
        record, turns = multiturn.run_chat(StubClient(response({"choices": []})), "switchyard", "backend", 1, args())
        self.assertTrue(record["error"].startswith("unexpected response"))
        self.assertEqual(turns[-1]["error"], record["error"])
        self.assertFalse(record["passed"])

    def test_body_without_message_is_a_turn_error(self):
        record, _ = multiturn.run_chat(StubClient(response({"choices": [{"text": "x"}]})), "switchyard",
                                       "backend", 1, args())
        self.assertIn("unexpected response", record["error"])

    def test_unpriced_turn_stops_the_conversation_when_capped(self):
        client = StubClient(response(reply(), model="vendor.other"))
        record, turns = multiturn.run_chat(client, "switchyard", "backend", 1, args())
        self.assertEqual(len(turns), 1)
        self.assertIn("cannot be enforced", record["error"])
        self.assertIsNone(record["cost"])
        self.assertEqual(record["unpriced_models"], ["vendor.other"])

    def test_unpriced_turn_stops_the_conversation_under_a_spend_budget_alone(self):
        client = StubClient(response(reply(), model="vendor.other"))
        record, turns = multiturn.run_chat(client, "switchyard", "backend", 1,
                                           args("--max-conv-cost", "0", "--max-spend", "2"))
        self.assertEqual(len(turns), 1)
        self.assertIn("cannot be enforced", record["error"])

    def test_uncapped_conversation_runs_every_turn(self):
        client = StubClient(response(reply(), model="vendor.other"))
        record, turns = multiturn.run_chat(client, "switchyard", "backend", 1, args("--max-conv-cost", "0"))
        self.assertEqual(len(turns), len(multiturn.SCRIPTS_BY_ID["backend"]["turns"]))
        self.assertIsNone(record["error"])

    def test_priced_conversation_sums_turn_costs(self):
        record, turns = multiturn.run_chat(StubClient(response(reply())), "switchyard", "backend", 1, args())
        self.assertAlmostEqual(record["cost"], sum(t["cost"] for t in turns), places=6)
        self.assertEqual(record["checks_passed"], 2)  # the "canberra" and one-sentence checks

    def test_harness_error_is_recorded_and_does_not_stop_the_run(self):
        class Exploding:
            def chat(self, *a, **k):
                raise RuntimeError("boom")

        record, turns = multiturn.run_one(Exploding(), "switchyard", "chat", "backend", 1, args())
        self.assertEqual(turns, [])
        self.assertIn("harness", record["error"])
        self.assertIsNone(multiturn.stop_reason(record, 1.0, 5.0))


def tool_reply(*calls):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": list(calls)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


class RecordingClient:
    def __init__(self, *payloads):
        self.payloads, self.sent = list(payloads), []

    def chat(self, route, session, messages, max_tokens, tools=None):
        self.sent.append([dict(m) for m in messages])
        return response(self.payloads.pop(0))


READ_SLUG = {"id": "c1", "function": {"name": "read_file", "arguments": {"path": "slug.py"}}}


class CodingToolCallTests(unittest.TestCase):
    def coding(self, client):
        return multiturn.run_coding(client, "switchyard", "slugify", 1,
                                    args("--max-turns", "3", "--max-conv-cost", "0"))

    def test_malformed_call_ends_the_conversation_and_keeps_prior_turn_costs(self):
        client = RecordingClient(tool_reply(READ_SLUG), tool_reply({"id": "c2", "function": {"arguments": "{}"}}))
        record, turns = self.coding(client)
        self.assertEqual(len(turns), 2)
        self.assertTrue(record["error"].startswith("Malformed tool call: "))
        self.assertEqual(turns[-1]["error"], record["error"])
        self.assertIsNone(turns[0]["error"])
        self.assertGreater(turns[0]["cost"], 0)
        self.assertAlmostEqual(record["cost"], turns[0]["cost"] + turns[1]["cost"], places=6)
        self.assertEqual(len(client.sent), 2, "no request after the malformed call")

    def test_invalid_json_arguments_are_a_turn_error(self):
        bad = {"id": "c1", "function": {"name": "read_file", "arguments": "{not json"}}
        record, turns = self.coding(RecordingClient(tool_reply(bad)))
        self.assertEqual(len(turns), 1)
        self.assertIn("invalid JSON arguments for read_file", record["error"])

    def test_dict_arguments_are_used(self):
        done = {"choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
        client = RecordingClient(tool_reply(READ_SLUG), done)
        record, turns = self.coding(client)
        self.assertIsNone(record["error"])
        self.assertEqual(turns[0]["tool_calls"], ["read_file"])
        tool_messages = [m for m in client.sent[1] if m["role"] == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "c1")
        self.assertIn("def slugify", tool_messages[0]["content"])


class CollectTests(unittest.TestCase):
    def test_queued_conversations_are_cancelled_once_the_run_stops(self):
        done, running, queued = Future(), Future(), Future()
        running.set_running_or_notify_cancel()
        done.set_result(({"route": "switchyard"}, []))
        running.set_result(({"route": "claude-opus-5-5"}, []))  # finishes, so it is still recorded
        seen = []

        def handle(record, turns):
            seen.append(record["route"])
            return record["route"] == "switchyard"

        self.assertTrue(multiturn.collect([done, running, queued], handle))
        self.assertTrue(queued.cancelled())
        self.assertEqual(seen, ["switchyard", "claude-opus-5-5"])

    def test_nothing_is_cancelled_when_the_run_continues(self):
        futures = [Future(), Future()]
        for future in futures:
            future.set_result(({"route": "switchyard"}, []))
        self.assertFalse(multiturn.collect(futures, lambda record, turns: False))


class SummaryTests(unittest.TestCase):
    def test_rows_per_route(self):
        records = [
            {"route": "switchyard", "kind": "coding", "passed": True, "cost": 0.2},
            {"route": "switchyard", "kind": "chat", "checks_passed": 5, "checks_total": 6, "cost": 0.1},
            {"route": "claude-opus-5-5", "kind": "chat", "checks_passed": 6, "checks_total": 6, "cost": None,
             "unpriced_models": ["x"]},
            {"route": "nemotron-super-3-120b", "kind": "chat", "error": "harness: boom", "cost": None},
            {"route": "other-group", "kind": "chat", "cost": None, "unpriced_models": ["unknown (model or usage unavailable)"]},
        ]
        rows = multiturn.summary_rows(records)
        self.assertEqual([r["route"] for r in rows],
                         ["switchyard", "claude-opus-5-5", "nemotron-super-3-120b", "other-group"])
        self.assertEqual((rows[0]["code_pass"], rows[0]["chat_checks"]), ("1/1", "5/6"))
        self.assertAlmostEqual(rows[0]["per_coding"], 0.2)
        self.assertAlmostEqual(rows[0]["per_chat"], 0.1)
        self.assertAlmostEqual(rows[0]["total"], 0.3)
        self.assertIsNone(rows[1]["total"])
        self.assertIn("add prices to PRICES", rows[1]["note"])
        self.assertIn("harness error", rows[2]["note"])
        self.assertEqual(rows[3]["note"], "selected model or usage unavailable")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            multiturn.summarize(records)
        self.assertIn("ROUTE", out.getvalue())
        self.assertIn("n/a", out.getvalue())
        self.assertIn("0.300000", out.getvalue())


class AssertCheckTests(unittest.TestCase):
    def test_checks_run_with_home_in_their_directory_and_no_credentials(self):
        secrets = {"AWS_SECRET_ACCESS_KEY": "example", "LITELLM_MASTER_KEY": "sk-local"}
        with mock.patch.dict(os.environ, secrets):
            ok, detail = multiturn.run_asserts("import os\n", [
                "assert os.path.realpath(os.environ['HOME']) == os.getcwd()",
                "assert not [k for k in os.environ if k.startswith(('AWS_', 'LITELLM_'))]",
            ])
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
