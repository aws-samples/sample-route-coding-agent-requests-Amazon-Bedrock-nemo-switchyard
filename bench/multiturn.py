#!/usr/bin/env python3
"""Multi-turn cost comparison across the gateway's model groups.

Runs the same conversations through each model group: by default every group
from GET /v1/models, the routed group `switchyard` first, then the
single-model baseline groups in the same deployment. For the first coding
walkthrough, use bench/run.py.

Two kinds of conversation:

* coding  An agentic loop over each task in tasks/. The test output is fed
          back after every edit; the loop stops when the tests pass or after
          --max-turns model turns.
* chat    Scripted conversations of seven user turns that alternate simple
          turns (greeting, restate, reformat, quick fact) with hard ones
          (design, debug a snippet, write a function). The full history is
          replayed every turn, so prompt tokens grow as a real client's do.
          Turns with a cheap correctness check (expected text, word limit,
          bullets, or assertions against the returned code) record the result.

Each turn is priced from its own usage and the model in the
x-litellm-model-name header, with PRICES from bench/run.py. Unknown models and
cached input are unpriced. When --max-conv-cost or --max-spend is set, an
unpriced turn stops the conversation and the run, because its spend cannot be
counted; --max-conv-cost 0 removes the per-conversation cap. Output is JSONL
with one "turn" record per model call and one "conversation" record per
conversation, followed by a summary per model group.

Usage:
  export GATEWAY_URL=... LITELLM_MASTER_KEY=...   # from the stack outputs
  python3 bench/multiturn.py --tasks slugify --scripts backend --max-spend 2
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import HERE, TOOLS, model_groups, open_url, parse_tool_calls, run_tool, sandbox_env, task_names, unpriced_reason, usage_cost  # noqa: E402

DEFAULT_MAX_TURNS = 6
REQUEST_TIMEOUT = 600

# Throttling and transient failures: retry with exponential backoff and full
# jitter (delay drawn from [0, min(cap, base * 2^(attempt-1))]), never shorter
# than the server's Retry-After. LiteLLM retries twice internally as well.
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 60.0

CODING_SYSTEM = (
    "You are a coding agent. Use the tools to inspect files, edit code and run tests. "
    "Do not edit test files. Every write_file result includes the test output after the "
    "write, so prefer writing a complete new version of the module in one step. You have "
    "at most {max_turns} turns. Stop when all tests pass, then reply with a one-line summary."
)

CHAT_SYSTEM = (
    "You are a senior software engineer helping a colleague. Be concise. When asked for "
    "code, return one complete Python code block and no explanation unless asked for one."
)

# Four scripts x seven user turns. Simple and hard turns alternate. "check" is a
# cheap correctness signal: expect = any of these substrings (case-insensitive),
# max_words = reply length bound, bullets = reply contains a list, asserts = run
# against the function/class the reply defines, in a credential-stripped
# subprocess (not an OS sandbox).
CHAT_SCRIPTS: list[dict] = [
    {
        "id": "algorithms",
        "turns": [
            {
                "label": "greeting",
                "difficulty": "simple",
                "user": "Hi! I'm cleaning up a small Python utility library today. In two sentences, "
                "say hello and tell me how you can help.",
            },
            {
                "label": "design",
                "difficulty": "hard",
                "user": "Design a rate limiter for a public REST API: 100 requests per minute per API "
                "key, served by three stateless application servers behind a load balancer. Compare "
                "token bucket and sliding window log, pick one, and say where the counters live and why.",
            },
            {
                "label": "restate",
                "difficulty": "simple",
                "user": "Restate your recommendation in one sentence.",
                "check": {"max_words": 60},
            },
            {
                "label": "debug",
                "difficulty": "hard",
                "user": "This should return the average of every full window of size k, so "
                "moving_average([1, 2, 3, 4], 2) must return [1.5, 2.5, 3.5], but it returns four "
                "values and the last one is wrong. Find the bug and return the corrected function.\n\n"
                "```python\ndef moving_average(xs, k):\n    out = []\n    for i in range(len(xs)):\n"
                "        window = xs[i:i + k]\n        out.append(sum(window) / k)\n    return out\n```",
                "check": {
                    "function": "moving_average",
                    "asserts": [
                        "assert list(moving_average([1, 2, 3, 4], 2)) == [1.5, 2.5, 3.5]",
                        "assert list(moving_average([5], 1)) == [5.0]",
                        "assert list(moving_average([1, 2, 3], 5)) == []",
                    ],
                },
            },
            {
                "label": "reformat",
                "difficulty": "simple",
                "user": "Format your explanation of that bug as a bullet list with at most four bullets.",
                "check": {"bullets": True},
            },
            {
                "label": "write",
                "difficulty": "hard",
                "user": "Write a Python function top_k_frequent(words, k) that returns the k most frequent "
                "words in a list, most frequent first, ties broken alphabetically. Standard library "
                "only. Return just the code.",
                "check": {
                    "function": "top_k_frequent",
                    "asserts": [
                        "assert list(top_k_frequent(['a', 'b', 'a', 'c', 'b', 'a'], 2)) == ['a', 'b']",
                        "assert list(top_k_frequent(['x', 'y', 'z'], 2)) == ['x', 'y']",
                        "assert list(top_k_frequent(['b', 'a', 'b', 'a'], 1)) == ['a']",
                    ],
                },
            },
            {
                "label": "factual",
                "difficulty": "simple",
                "user": "Quick one: what is the time complexity of that function for n words? One line.",
                "check": {"expect": ["o(n", "n log"]},
            },
        ],
    },
    {
        "id": "backend",
        "turns": [
            {
                "label": "greeting",
                "difficulty": "simple",
                "user": "Hello! Quick warm-up before we start: what is the capital of Australia? One word.",
                "check": {"expect": ["canberra"]},
            },
            {
                "label": "design",
                "difficulty": "hard",
                "user": "Design the storage and caching for a product catalog service: 2 million products, "
                "50,000 reads per second, 200 writes per second, and a price change must be visible on "
                "the product page within 5 seconds. Say what you cache, where, with what TTL, and how "
                "you invalidate.",
            },
            {
                "label": "restate",
                "difficulty": "simple",
                "user": "Restate that in one sentence.",
                "check": {"max_words": 60},
            },
            {
                "label": "debug",
                "difficulty": "hard",
                "user": "This should drop duplicates while keeping first-occurrence order, so "
                "dedupe([3, 1, 3, 2, 1]) must return [3, 1, 2], but it returns an empty list. Find "
                "the bug and return the corrected function.\n\n"
                "```python\ndef dedupe(items):\n    seen = set()\n    result = []\n    for item in items:\n"
                "        if item in seen:\n            seen.add(item)\n            result.append(item)\n"
                "    return result\n```",
                "check": {
                    "function": "dedupe",
                    "asserts": [
                        "assert list(dedupe([3, 1, 3, 2, 1])) == [3, 1, 2]",
                        "assert list(dedupe([])) == []",
                        "assert list(dedupe(['a', 'a'])) == ['a']",
                    ],
                },
            },
            {
                "label": "reformat",
                "difficulty": "simple",
                "user": "Turn your explanation of that bug into a bullet list, three bullets at most.",
                "check": {"bullets": True},
            },
            {
                "label": "write",
                "difficulty": "hard",
                "user": "Write a Python function parse_duration(s) that turns strings like '1h30m', '45s', "
                "'2h' or '1h2m3s' into a total number of seconds as an int. Each of the units h, m and s "
                "may appear at most once and only in that order. Raise ValueError for anything else, "
                "including an empty string. Standard library only. Return just the code.",
                "check": {
                    "function": "parse_duration",
                    "asserts": [
                        "assert parse_duration('1h30m') == 5400",
                        "assert parse_duration('45s') == 45",
                        "assert parse_duration('1h2m3s') == 3723",
                        "assert parse_duration('2h') == 7200",
                        "assert _raises(ValueError, parse_duration, '30m1h')",
                        "assert _raises(ValueError, parse_duration, '')",
                    ],
                },
            },
            {
                "label": "factual",
                "difficulty": "simple",
                "user": "How many seconds is 1h2m3s? Just the number.",
                "check": {"expect": ["3723"]},
            },
        ],
    },
    {
        "id": "concurrency",
        "turns": [
            {
                "label": "greeting",
                "difficulty": "simple",
                "user": "Hi there. In one sentence, what is the difference between a process and a thread?",
                "check": {"expect": ["memory", "address space"]},
            },
            {
                "label": "design",
                "difficulty": "hard",
                "user": "Design a background job system in Python for about 500 jobs per minute where some "
                "jobs run for 10 minutes. Every job must run at least once and a crashed worker must not "
                "lose jobs. Cover the queue choice, visibility timeouts or leases, retries with backoff, "
                "idempotency, and how you detect a stuck worker.",
            },
            {
                "label": "restate",
                "difficulty": "simple",
                "user": "Summarize that design in one sentence.",
                "check": {"max_words": 60},
            },
            {
                "label": "debug",
                "difficulty": "hard",
                "user": "Calling Counter().add(3) hangs forever. Find the bug and return the corrected "
                "class.\n\n"
                "```python\nimport threading\n\nclass Counter:\n    def __init__(self):\n"
                "        self.value = 0\n        self.lock = threading.Lock()\n\n"
                "    def increment(self):\n        with self.lock:\n            self.value += 1\n\n"
                "    def add(self, n):\n        for _ in range(n):\n            self.lock.acquire()\n"
                "            self.value += 1\n```",
                "check": {
                    "function": "Counter",
                    "asserts": [
                        "c = Counter()",
                        "c.add(3)",
                        "c.increment()",
                        "assert c.value == 4",
                    ],
                },
            },
            {
                "label": "reformat",
                "difficulty": "simple",
                "user": "Rewrite that explanation as a bullet list with at most three bullets.",
                "check": {"bullets": True},
            },
            {
                "label": "write",
                "difficulty": "hard",
                "user": "Write a Python function merge_intervals(intervals) that takes a list of "
                "[start, end] integer pairs (inclusive) and returns the merged, sorted list of "
                "non-overlapping intervals. Intervals that only touch, like [1, 3] and [4, 5], stay "
                "separate; [1, 3] and [3, 5] merge into [1, 5]. Return just the code.",
                "check": {
                    "function": "merge_intervals",
                    "asserts": [
                        "assert [list(x) for x in merge_intervals([[1, 3], [2, 6], [8, 10], [15, 18]])]"
                        " == [[1, 6], [8, 10], [15, 18]]",
                        "assert [list(x) for x in merge_intervals([[1, 3], [4, 5]])] == [[1, 3], [4, 5]]",
                        "assert [list(x) for x in merge_intervals([[1, 3], [3, 5]])] == [[1, 5]]",
                        "assert [list(x) for x in merge_intervals([])] == []",
                    ],
                },
            },
            {
                "label": "factual",
                "difficulty": "simple",
                "user": "What is the time complexity of merge_intervals for n intervals? One line.",
                "check": {"expect": ["log"]},
            },
        ],
    },
    {
        "id": "api",
        "turns": [
            {
                "label": "greeting",
                "difficulty": "simple",
                "user": "Hey! Can you help me with a small text-processing module today? One short "
                "sentence is fine.",
            },
            {
                "label": "design",
                "difficulty": "hard",
                "user": "Design the versioning and deprecation strategy for a public JSON REST API used by "
                "about 300 external integrators. Cover URL versus header versioning, how long old "
                "versions stay supported, how breaking changes are communicated, and how you measure "
                "who is still on an old version.",
            },
            {
                "label": "restate",
                "difficulty": "simple",
                "user": "Give me the one-sentence version of that.",
                "check": {"max_words": 60},
            },
            {
                "label": "debug",
                "difficulty": "hard",
                "user": "chunk([1, 2, 3, 4, 5], 2) should return [[1, 2], [3, 4], [5]] but it drops "
                "elements. Find the bug and return the corrected function.\n\n"
                "```python\ndef chunk(items, size):\n    chunks = []\n"
                "    for i in range(0, len(items), size):\n        chunks.append(items[i:i + size - 1])\n"
                "    return chunks\n```",
                "check": {
                    "function": "chunk",
                    "asserts": [
                        "assert [list(c) for c in chunk([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]",
                        "assert list(chunk([], 3)) == []",
                        "assert [list(c) for c in chunk([1, 2, 3], 3)] == [[1, 2, 3]]",
                    ],
                },
            },
            {
                "label": "reformat",
                "difficulty": "simple",
                "user": "Now format that explanation as a bullet list, three bullets at most.",
                "check": {"bullets": True},
            },
            {
                "label": "write",
                "difficulty": "hard",
                "user": "Write a Python function roman_to_int(s) that converts a Roman numeral (symbols "
                "I, V, X, L, C, D, M with standard subtractive notation) to an int for values 1 to 3999. "
                "Raise ValueError for an empty string or any character that is not one of those "
                "symbols. Return just the code.",
                "check": {
                    "function": "roman_to_int",
                    "asserts": [
                        "assert roman_to_int('XIV') == 14",
                        "assert roman_to_int('MCMXCIV') == 1994",
                        "assert roman_to_int('MMMCMXCIX') == 3999",
                        "assert _raises(ValueError, roman_to_int, '')",
                        "assert _raises(ValueError, roman_to_int, 'ABC')",
                    ],
                },
            },
            {
                "label": "factual",
                "difficulty": "simple",
                "user": "What is MCMXCIV in decimal? Just the number.",
                "check": {"expect": ["1994"]},
            },
        ],
    },
]
SCRIPTS_BY_ID = {s["id"]: s for s in CHAT_SCRIPTS}

FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.S)
BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+\S", re.M)
CHECK_PRELUDE = (
    "def _raises(exc, fn, *args):\n"
    "    try:\n"
    "        fn(*args)\n"
    "    except exc:\n"
    "        return True\n"
    "    return False\n"
)


# --- correctness signals -----------------------------------------------------


def extract_code(content: str, name: str) -> str | None:
    """The last fenced block that defines `name`, or the whole reply if it is bare code."""
    defines = re.compile(rf"^\s*(?:def|class)\s+{re.escape(name)}\b", re.M)
    blocks = [b for b in FENCE.findall(content) if defines.search(b)]
    if blocks:
        return blocks[-1]
    return content if defines.search(content) else None


def run_asserts(code: str, asserts: list[str]) -> tuple[bool, str]:
    """Run model-written code plus the script's assertions in a credential-stripped subprocess
    (not an OS sandbox), with HOME set to its temporary directory."""
    program = code.rstrip() + "\n\n" + CHECK_PRELUDE + "\n" + "\n".join(asserts) + "\nprint('CHECKS OK')\n"
    with tempfile.TemporaryDirectory(prefix="chatcheck-") as d:
        path = Path(d) / "check.py"
        path.write_text(program)
        try:
            # Fixed argv with no shell: this interpreter runs the generated check file.
            p = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                [sys.executable, "-I", str(path)],
                cwd=d,
                capture_output=True,
                text=True,
                timeout=10,
                env=sandbox_env(home=d),
            )
        except subprocess.TimeoutExpired:
            return False, "timed out after 10 seconds"
    if p.returncode == 0 and "CHECKS OK" in p.stdout:
        return True, ""
    lines = p.stderr.strip().splitlines()
    return False, (lines[-1] if lines else f"exit {p.returncode}")[:200]


def run_check(check: dict, content: str) -> tuple[str, bool, str]:
    if "asserts" in check:
        code = extract_code(content, check["function"])
        if code is None:
            return "asserts", False, f"no code defining {check['function']}"
        ok, detail = run_asserts(code, check["asserts"])
        return "asserts", ok, detail
    if "expect" in check:
        low = content.lower()
        return "expect", any(e.lower() in low for e in check["expect"]), ""
    if "max_words" in check:
        n = len(content.split())
        return "max_words", n <= check["max_words"], f"{n} words"
    if "bullets" in check:
        return "bullets", bool(BULLET.search(content)), ""
    raise ValueError(f"unknown check {check}")


def last_line(output: str) -> str:
    lines = output.rstrip().splitlines()
    return (lines[-1] if lines else "")[:120]


def tests_pass(output: str) -> bool:
    return last_line(output).startswith("OK")


# --- gateway client -----------------------------------------------------------


def backoff_delay(attempt: int, retry_after: str | None) -> float:
    """Seconds to wait before retry number `attempt` (1-based): full jitter, Retry-After floor."""
    ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
    delay = random.uniform(0, ceiling)
    if retry_after:
        try:
            delay = max(delay, min(BACKOFF_CAP_S, float(retry_after)))
        except ValueError:
            pass
    return delay


class Client:
    def __init__(self, url: str, key: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.max_attempts = max(1, max_attempts)

    def _request(self, method: str, path: str, session: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path,
            data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "x-request-id": session,
            },
        )
        with open_url(req, timeout=REQUEST_TIMEOUT) as resp:
            headers = {
                k.lower(): v
                for k, v in resp.headers.items()
                if k.lower().startswith(("x-litellm",))
            }
            return resp.status, headers, json.load(resp)

    def chat(self, route: str, session: str, messages: list[dict], max_tokens: int, tools=None) -> dict:
        """One chat completion. Retries throttling and transient failures with backoff."""
        # No temperature/seed: some Bedrock models (Kimi K3) reject the temperature
        # field, and every configuration must receive the identical request.
        body = {"model": route, "max_tokens": max_tokens, "messages": messages}
        if tools:
            body["tools"] = tools
        result = {
            "http_status": None,
            "attempts": 0,
            "retry_wait_ms": 0,
            "retry_statuses": [],
            "latency_ms": None,
            "router_headers": {},
            "payload": None,
            "error": None,
        }
        for attempt in range(1, self.max_attempts + 1):
            result["attempts"] = attempt
            retry_after = None
            t0 = time.monotonic()
            try:
                status, headers, payload = self._request("POST", "/v1/chat/completions", session, body)
                result.update(
                    http_status=status,
                    latency_ms=round((time.monotonic() - t0) * 1000),
                    router_headers=headers,
                    payload=payload,
                    error=None,
                )
                return result
            except urllib.error.HTTPError as e:
                detail = e.read()[:300]
                retry_after = e.headers.get("Retry-After") if e.headers else None
                result.update(
                    http_status=e.code,
                    latency_ms=round((time.monotonic() - t0) * 1000),
                    error=f"HTTP {e.code}: {detail!r}",
                )
                # LiteLLM wraps a throttled Bedrock call in a 5xx body; treat it as throttling.
                throttled = e.code == 429 or b"Throttling" in detail or b"TooManyRequests" in detail
                if e.code not in RETRY_STATUSES and not throttled:
                    return result
                result["retry_statuses"].append(e.code)
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                http.client.HTTPException,
                OSError,
            ) as e:
                result.update(
                    http_status=None,
                    latency_ms=round((time.monotonic() - t0) * 1000),
                    error=f"request failed: {e}",
                )
                result["retry_statuses"].append(None)
            if attempt == self.max_attempts:
                break
            delay = backoff_delay(attempt, retry_after)
            result["retry_wait_ms"] += round(delay * 1000)
            time.sleep(delay)
        return result

    def model_groups(self) -> list[str]:
        """Model groups the gateway serves, with the routed group first."""
        return model_groups(lambda: self._request("GET", "/v1/models", f"models-{uuid.uuid4().hex[:8]}")[2],
                            self.url)


# --- records ------------------------------------------------------------------


def reply_message(payload: object) -> dict | None:
    """The first choice's message, or None when a 200 body has none."""
    try:
        message = payload["choices"][0]["message"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None
    return message if isinstance(message, dict) else None


def unexpected_response(payload: object) -> str:
    return f"unexpected response: {json.dumps(payload)[:300]}"


def turn_record(base: dict, turn: int, resp: dict, messages_sent: int) -> dict:
    payload = resp.get("payload") if isinstance(resp.get("payload"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = reply_message(payload) or {}
    # The body's model is the group name; the header names the model that answered.
    model_header = resp["router_headers"].get("x-litellm-model-name")
    model_body = payload.get("model")
    model = model_header
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    if resp["error"] and not model and not prompt_tokens and not completion_tokens:
        turn_cost = 0.0  # the request failed with no answering model and no usage: nothing to price
    else:
        turn_cost = usage_cost(model, usage)
    return {
        "record": "turn",
        **base,
        "turn": turn,
        "messages_sent": messages_sent,
        "http_status": resp["http_status"],
        "attempts": resp["attempts"],
        "retry_statuses": resp["retry_statuses"],
        "retry_wait_ms": resp["retry_wait_ms"],
        "latency_ms": resp["latency_ms"],
        "model_header": model_header,
        "model_body": model_body,
        "model": model,
        "router_headers": resp["router_headers"],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": prompt_details.get("cached_tokens") or usage.get("cache_read_input_tokens") or 0,
        "cache_creation_tokens": prompt_details.get("cache_creation_tokens")
        or usage.get("cache_creation_input_tokens")
        or 0,
        "reasoning_tokens": completion_details.get("reasoning_tokens") or 0,
        "finish_reason": choice.get("finish_reason"),
        "response_preview": (message.get("content") or "")[:300],
        "unpriced_reason": None if turn_cost is not None else unpriced_reason(model, usage),
        "cost": turn_cost,
        "error": resp["error"],
    }


def conversation_record(base: dict, turns: list[dict], error: str | None, start: float, **extra) -> dict:
    turn_models = {t["model"] for t in turns if t["model"]}
    unpriced = unpriced_models(turns)
    return {
        "record": "conversation",
        **base,
        "turns": len(turns),
        "error": error,
        **extra,
        "models": sorted(turn_models),
        "turn_models": [t["model"] for t in turns],
        "prompt_tokens": sum(t["prompt_tokens"] for t in turns),
        "completion_tokens": sum(t["completion_tokens"] for t in turns),
        "cached_tokens": sum(t["cached_tokens"] for t in turns),
        "cost": None if unpriced else round(sum(t["cost"] for t in turns), 6),
        "unpriced_models": unpriced,
        "seconds": round(time.monotonic() - start, 1),
    }


UNPRICED_SUFFIX = {"cached": " (cached input)", "unavailable": " (model or usage unavailable)"}


def unpriced_models(turns: list[dict]) -> list[str]:
    """Models whose turns could not be priced, marked when the cause is cached input or missing data."""
    return sorted({(t["model"] or "unknown") + UNPRICED_SUFFIX.get(t.get("unpriced_reason"), "")
                   for t in turns if t["cost"] is None})


def over_budget(turns: list[dict], cap: float | None, max_spend: float | None = None) -> bool:
    """True past the conversation cap, or, when any budget is set, as soon as a turn is unpriced:
    its spend cannot be counted. --max-spend itself is checked between conversations."""
    if cap is None and max_spend is None:
        return False
    if unpriced_models(turns):
        return True
    return cap is not None and sum(t["cost"] for t in turns) > cap


def budget_error(turns: list[dict], cap: float | None) -> str:
    unpriced = unpriced_models(turns)
    if unpriced:
        return (f"budget: no price for {unpriced}, so --max-conv-cost and --max-spend cannot be enforced "
                "(add prices to PRICES in bench/run.py, or run with --max-conv-cost 0 and no --max-spend)")
    return f"budget: conversation cost cap {cap} reached"


# --- conversations ------------------------------------------------------------


def run_coding(client: Client, route: str, task: str, rep: int, args) -> tuple[dict, list[dict]]:
    session = f"mt-{route}-{task}-{uuid.uuid4().hex[:8]}"
    base = {
        "run": args.tag,
        "route": route,
        "kind": "coding",
        "conversation": task,
        "rep": rep,
        "session": session,
    }
    root = Path(tempfile.mkdtemp(prefix=f"{task}-")).resolve()
    shutil.copytree(HERE / "tasks" / task, root, dirs_exist_ok=True)
    files = sorted(p.name for p in root.iterdir() if p.is_file() and p.name != "TASK.md")
    prompt = (root / "TASK.md").read_text().strip() + "\n\nProject files:\n"
    for name in files:
        prompt += f"\n### {name}\n```python\n{(root / name).read_text()}```\n"
    messages: list[dict] = [
        {"role": "system", "content": CODING_SYSTEM.format(max_turns=args.max_turns)},
        {"role": "user", "content": prompt},
    ]
    test_output = run_tool(root, "run_tests", {})
    turns: list[dict] = []
    error = None
    start = time.monotonic()
    for turn in range(1, args.max_turns + 1):
        resp = client.chat(route, session, messages, args.coding_max_tokens, TOOLS)
        rec = turn_record(base, turn, resp, len(messages))
        rec.update(tool_calls=[], wrote=False, test_result=last_line(test_output), tests_passed=tests_pass(test_output))
        if resp["error"]:
            turns.append(rec)
            error = resp["error"]
            break
        msg = reply_message(resp["payload"])
        if msg is None:
            error = rec["error"] = unexpected_response(resp["payload"])
            turns.append(rec)
            break
        calls = msg.get("tool_calls") or []
        try:
            parsed = parse_tool_calls(calls)
        except ValueError as exc:
            error = rec["error"] = f"Malformed tool call: {exc}"
            turns.append(rec)
            break
        messages.append(
            {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls") and v is not None}
        )
        wrote = False
        tests_after_write = True
        last_write_index = -1
        for call_id, name, call_args in parsed:
            out = run_tool(root, name, call_args)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": out})
            if name == "write_file" and out.startswith("Wrote"):
                wrote = True
                tests_after_write = False
                last_write_index = len(messages) - 1
            elif name == "run_tests":
                test_output = out
                tests_after_write = True
        if wrote and not tests_after_write:
            # The agent edited without testing: run the tests and feed the output
            # back with the write result, so every edit turn gets test feedback.
            test_output = run_tool(root, "run_tests", {})
            messages[last_write_index]["content"] += "\n\n$ python3 -m unittest\n" + test_output
        rec.update(
            tool_calls=[name for _, name, _ in parsed],
            wrote=wrote,
            test_result=last_line(test_output),
            tests_passed=tests_pass(test_output),
        )
        turns.append(rec)
        if rec["tests_passed"] or not calls:
            break
        if over_budget(turns, args.max_conv_cost, args.max_spend):
            error = budget_error(turns, args.max_conv_cost)
            break

    passed = tests_pass(run_tool(root, "run_tests", {}))
    shutil.rmtree(root)
    record = conversation_record(base, turns, error, start, passed=passed)
    return record, turns


def run_chat(client: Client, route: str, script_id: str, rep: int, args) -> tuple[dict, list[dict]]:
    script = SCRIPTS_BY_ID[script_id]
    session = f"mt-{route}-{script_id}-{uuid.uuid4().hex[:8]}"
    base = {
        "run": args.tag,
        "route": route,
        "kind": "chat",
        "conversation": script_id,
        "rep": rep,
        "session": session,
    }
    messages: list[dict] = [{"role": "system", "content": CHAT_SYSTEM}]
    turns: list[dict] = []
    error = None
    checks_total = checks_passed = 0
    start = time.monotonic()
    for turn, step in enumerate(script["turns"], 1):
        messages.append({"role": "user", "content": step["user"]})
        resp = client.chat(route, session, messages, args.chat_max_tokens)
        rec = turn_record(base, turn, resp, len(messages))
        rec.update(
            label=step["label"],
            difficulty=step["difficulty"],
            check=None,
            check_passed=None,
            check_detail=None,
            response_chars=0,
            response_text=None,
        )
        if resp["error"]:
            turns.append(rec)
            error = resp["error"]
            break
        msg = reply_message(resp["payload"])
        if msg is None:
            error = rec["error"] = unexpected_response(resp["payload"])
            turns.append(rec)
            break
        content = msg.get("content") or ""
        messages.append({"role": "assistant", "content": content})
        rec.update(response_chars=len(content), response_text=content)
        if step.get("check"):
            kind, ok, detail = run_check(step["check"], content)
            rec.update(check=kind, check_passed=ok, check_detail=detail)
            checks_total += 1
            checks_passed += int(ok)
        turns.append(rec)
        if over_budget(turns, args.max_conv_cost, args.max_spend):
            error = budget_error(turns, args.max_conv_cost)
            break
    record = conversation_record(
        base,
        turns,
        error,
        start,
        passed=error is None and checks_passed == checks_total,
        checks_passed=checks_passed,
        checks_total=checks_total,
    )
    return record, turns


def run_one(client: Client, route: str, kind: str, conversation: str, rep: int, args) -> tuple[dict, list[dict]]:
    try:
        if kind == "coding":
            return run_coding(client, route, conversation, rep, args)
        return run_chat(client, route, conversation, rep, args)
    except Exception:  # keep one bad response from ending a long run
        return (
            {
                "record": "conversation",
                "run": args.tag,
                "route": route,
                "kind": kind,
                "conversation": conversation,
                "rep": rep,
                "session": None,
                "turns": 0,
                "error": "harness: " + traceback.format_exc()[-600:],
                "passed": False,
                "models": [],
                "cost": None,
                "unpriced_models": [],
                "seconds": 0.0,
            },
            [],
        )


# --- reporting ----------------------------------------------------------------


def stop_reason(record: dict, max_conv_cost: float | None, max_spend: float | None) -> str | None:
    """Why the run must stop after this conversation, or None to keep going.

    Only unpriced turns stop it, and only when a budget is set: their spend cannot be
    counted against the budget. A harness error (no turns recorded) is logged and the
    run continues.
    """
    models = record.get("unpriced_models")
    if not models or (max_conv_cost is None and max_spend is None):
        return None
    return (f"budget: no price for {models}, so --max-spend and --max-conv-cost cannot be enforced; "
            "not starting more conversations (add prices to PRICES in bench/run.py)")


def collect(futures: list[Future], handle: Callable[[dict, list[dict]], bool]) -> bool:
    """Handle each conversation in submission order. Once handle() asks to stop, cancel the
    queued rest; conversations already running still finish and are handled. True if stopped."""
    stopped = False
    for future in futures:
        if future.cancelled():
            continue
        if handle(*future.result()) and not stopped:
            stopped = True
            for other in futures:
                other.cancel()
    return stopped


def conversation_cost(r: dict) -> float:
    """Priced spend only. A record whose cost is unknown counts 0.0 here; the summary prints n/a for its route."""
    return r.get("cost") or 0.0


def summary_rows(records: list[dict]) -> list[dict]:
    """Per model group: coding pass rate, chat checks, and mean cost per conversation (None when unpriced)."""
    by_route: dict[str, list[dict]] = {}
    for r in records:
        by_route.setdefault(r["route"], []).append(r)
    rows = []
    for route, rs in by_route.items():
        coding = [r for r in rs if r["kind"] == "coding"]
        chat = [r for r in rs if r["kind"] == "chat"]
        row = {
            "route": route,
            "conversations": len(rs),
            "code_pass": f"{sum(1 for r in coding if r.get('passed'))}/{len(coding)}",
            "chat_checks": f"{sum(r.get('checks_passed', 0) for r in chat)}/{sum(r.get('checks_total', 0) for r in chat)}",
            "per_coding": None, "per_chat": None, "total": None, "note": "",
        }
        if any(r.get("cost") is None for r in rs):
            notes = []
            labels = [m for r in rs for m in r.get("unpriced_models") or []]
            if any(not m.endswith(tuple(UNPRICED_SUFFIX.values())) for m in labels):
                notes.append("add prices to PRICES")
            if any(m.endswith(UNPRICED_SUFFIX["cached"]) for m in labels):
                notes.append("cached input is not priced")
            if any(m.endswith(UNPRICED_SUFFIX["unavailable"]) for m in labels):
                notes.append("selected model or usage unavailable")
            if any(str(r.get("error") or "").startswith("harness") for r in rs):
                notes.append("harness error, see records")
            row["note"] = "; ".join(notes)
        else:
            row["per_coding"] = sum(map(conversation_cost, coding)) / len(coding) if coding else None
            row["per_chat"] = sum(map(conversation_cost, chat)) / len(chat) if chat else None
            row["total"] = sum(map(conversation_cost, rs))
        rows.append(row)
    return rows


def summarize(records: list[dict]) -> None:
    def usd(value: float | None, width: int = 10) -> str:
        return f"{'n/a' if value is None else f'{value:.6f}':>{width}s}"

    print("\nSummary (USD estimates from per-turn usage and the model that answered; cached input is unpriced)")
    print(f"{'ROUTE':24s} {'CONVS':>5s} {'CODE PASS':>9s} {'CHAT CHECKS':>11s} "
          f"{'$/CODING':>10s} {'$/CHAT':>10s} {'TOTAL $':>10s}")
    for row in summary_rows(records):
        line = (f"{row['route']:24s} {row['conversations']:5d} {row['code_pass']:>9s} {row['chat_checks']:>11s} "
                f"{usd(row['per_coding'])} {usd(row['per_chat'])} {usd(row['total'])}")
        print(line + (f" ({row['note']})" if row["note"] else ""))


def conv_cap(value: float) -> float | None:
    """--max-conv-cost: 0 means no cap."""
    return None if value == 0 else value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", nargs="+", default=None,
                        help="model groups (default: every group from GET /v1/models, switchyard first)")
    parser.add_argument(
        "--tasks",
        nargs="*",
        default=task_names(),
        choices=task_names(),
        help="coding tasks from tasks/ (empty list: skip coding)",
    )
    parser.add_argument(
        "--scripts",
        nargs="*",
        default=list(SCRIPTS_BY_ID),
        choices=list(SCRIPTS_BY_ID),
        help="chat scripts (empty list: skip chat)",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help="model turns per coding task")
    parser.add_argument("--coding-max-tokens", type=int, default=8192)
    parser.add_argument("--chat-max-tokens", type=int, default=1024)
    parser.add_argument("--max-conv-cost", type=float, default=1.0, help="USD cap per conversation (0: no cap)")
    parser.add_argument("--max-spend", type=float, default=None, help="USD; stop starting conversations past this")
    parser.add_argument("--parallel", type=int, default=1, help="model groups run concurrently per conversation")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="attempts per model call on throttling or transient failures (exponential backoff, full jitter)",
    )
    parser.add_argument("--tag", default="", help="suffix for the results file name")
    parser.add_argument("--out-dir", type=Path, default=HERE / "results")
    args = parser.parse_args(argv)
    if args.max_conv_cost < 0:
        parser.error("--max-conv-cost must be 0 (no cap) or positive")
    if min(args.repeats, args.max_turns) < 1:
        parser.error("--repeats and --max-turns must be positive")
    args.max_conv_cost = conv_cap(args.max_conv_cost)
    return args


def main() -> None:
    args = parse_args()

    url = os.environ.get("GATEWAY_URL")
    key = os.environ.get("LITELLM_MASTER_KEY")
    if not url or not key:
        sys.exit("Set GATEWAY_URL and LITELLM_MASTER_KEY (see the stack outputs).")
    client = Client(url, key, max_attempts=args.max_attempts)
    routes = args.routes or client.model_groups()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{args.tag}" if args.tag else ""
    out = args.out_dir / f"multiturn-{time.strftime('%Y%m%d-%H%M%S')}{suffix}.jsonl"
    conversations = [("coding", t) for t in args.tasks] + [("chat", s) for s in args.scripts]
    records: list[dict] = []
    spent = 0.0
    stopped = False
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        for rep in range(1, args.repeats + 1):
            for kind, conversation in conversations:
                if args.max_spend is not None and spent >= args.max_spend:
                    print(f"budget: spent ${spent:.6f} >= --max-spend {args.max_spend}; not starting more conversations")
                    stopped = True
                    break
                futures = [pool.submit(run_one, client, route, kind, conversation, rep, args) for route in routes]

                def handle(record: dict, turns: list[dict], kind: str = kind, conversation: str = conversation,
                           rep: int = rep) -> bool:
                    nonlocal spent
                    records.append(record)
                    with out.open("a") as f:
                        for t in turns:
                            f.write(json.dumps(t) + "\n")
                        f.write(json.dumps(record) + "\n")
                    spent += conversation_cost(record)
                    outcome = (
                        f"passed={record.get('passed')!s:5s}"
                        if kind == "coding"
                        else f"checks={record.get('checks_passed', 0)}/{record.get('checks_total', 0)}"
                    )
                    c = record.get("cost")
                    print(
                        f"{record['route']:24s} {kind:6s} {conversation:11s} rep={rep} turns={record['turns']:2d} "
                        f"{outcome} cost={'n/a' if c is None else f'${c:.6f}'} models={record.get('models')} "
                        f"{(record.get('error') or '')[:160]}",
                        flush=True,
                    )
                    reason = stop_reason(record, args.max_conv_cost, args.max_spend)
                    if reason:
                        print(reason, flush=True)
                    return reason is not None

                stopped = collect(futures, handle)
                if stopped:
                    break
            if stopped:
                break
    summarize(records)
    print(f"\nTotal priced spend this run: ${spent:.6f}")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
