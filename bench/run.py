#!/usr/bin/env python3
"""Run visible coding tasks through the gateway's model groups and compare cost per task.

Examples:
  python3 bench/run.py --list-tasks
  python3 bench/run.py --tasks slugify --model switchyard --max-turns 6
  python3 bench/run.py --tasks slugify roman --repeats 3

Without --model or --routes, every model group from GET /v1/models runs, the
routed group `switchyard` first, then the single-model baseline groups. After
the runs, a summary per model group shows RUNS, SOLVED, ERRORS, COST/TASK,
and COST/SOLVED. Each run preserves its input files, workspace, transcript, patch,
and result; one summary record per run is appended to bench/results/run-*.jsonl.

Uses the Python standard library. Model requests incur provider charges.
"""
from __future__ import annotations

import argparse
import difflib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
MAX_TURNS = 6
ROUTED_GROUP = "switchyard"
# Model alias -> (Amazon Bedrock model ID, USD per 1M input tokens, USD per 1M
# output tokens). Standard on-demand prices in us-east-1 from the AWS Price List,
# checked 2026-10-22. Claude Opus 5.5 is priced for the global cross-Region
# inference profile the profiles use (in-Region on-demand is $4.40 / $22.00).
# Check the Amazon Bedrock pricing page for your Region and add an entry when
# you add a model to a profile. These are estimates, not billing
# records. Turns with cached input or an unknown model are left unpriced.
PRICES: dict[str, tuple[str, float, float]] = {
    "claude-opus-5-5": ("anthropic.claude-opus-5-5", 4.0, 20.0),
    "nemotron-super-3-120b": ("nvidia.nemotron-super-3-120b", 0.15, 0.65),
}
SYSTEM = (
    "You are a coding agent. Use the tools to inspect files, edit code and run tests. "
    "Do not edit test files. Stop when all tests pass, then reply with a one-line summary."
)
TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List the project files.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a project file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Replace a project source file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_tests", "description": "Run python3 -m unittest.",
        "parameters": {"type": "object", "properties": {}}}},
]


def canonical_model(model: str) -> str:
    if model.startswith("bedrock/"):
        model = model[len("bedrock/"):]
    for prefix in ("converse/", "invoke/", "chat_completions/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    return model


def price_for(model: str) -> tuple[float, float] | None:
    """Price by exact alias, or by the longest Bedrock model ID the model string contains.

    The ID must end the string or be followed by ":" or a "-v<digit>" version
    suffix, so a variant such as ...-thinking is not priced as the base model.
    Date-suffixed IDs (...-20260101-v1:0) do not match: add a PRICES entry for them.
    """
    if model in PRICES:
        _, price_in, price_out = PRICES[model]
        return price_in, price_out
    matches = [(len(model_id), price_in, price_out) for model_id, price_in, price_out in PRICES.values()
               if re.search(re.escape(model_id) + r"(?=$|:|-v\d)", model)]
    if not matches:
        return None
    _, price_in, price_out = max(matches)
    return price_in, price_out


def cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    price = price_for(model)
    return None if price is None else (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000


def has_cached_input(usage: dict) -> bool:
    """True when the response reports cache reads or writes, which are billed at other rates."""
    details = usage.get("prompt_tokens_details") or {}
    if any(details.get(k, 0) for k in ("cached_tokens", "cache_creation_tokens", "cache_write_tokens")):
        return True
    return bool(usage.get("cache_creation_input_tokens") or usage.get("cache_read_input_tokens"))


def unpriced_reason(model: str | None, usage: dict) -> str | None:
    """Why a turn has no estimate: "unavailable" (no selected-model header or integer token
    counts), "cached" (cached input), "price" (no PRICES entry), or None when it is priced."""
    if not model or any(not isinstance(usage.get(k), int) for k in ("prompt_tokens", "completion_tokens")):
        return "unavailable"
    if has_cached_input(usage):
        return "cached"
    return "price" if price_for(model) is None else None


def usage_cost(model: str | None, usage: dict) -> float | None:
    if not model or any(not isinstance(usage.get(k), int) for k in ("prompt_tokens", "completion_tokens")):
        return None
    if has_cached_input(usage):
        return None
    return cost(model, usage["prompt_tokens"], usage["completion_tokens"])


def open_url(request: urllib.request.Request | str, timeout: float):
    """urllib.request.urlopen for http and https URLs only; any other scheme raises ValueError."""
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"Only http and https URLs are allowed, not scheme {scheme!r}")
    # The scheme check above keeps file: and custom schemes away from urlopen.
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected


def sandbox_env(home: str | None = None) -> dict[str, str]:
    """Strip provider credentials, and point HOME at `home` when given so ~/.aws is not found.

    This is not an operating-system sandbox."""
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
    if home is not None:
        env["HOME"] = home
    return env


def run_tool(root: Path, name: str, args: dict) -> str:
    if name == "run_tests":
        # Model-written code runs on a copy outside the repository, with HOME set to the copy.
        with tempfile.TemporaryDirectory(prefix="bench-tests-") as temporary:
            shutil.copytree(root, temporary, dirs_exist_ok=True)
            try:
                # Fixed argv with no shell: this interpreter runs the copied unit tests.
                result = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                    [sys.executable, "-I", "-m", "unittest"], cwd=temporary, capture_output=True,
                    text=True, timeout=120, env=sandbox_env(home=temporary))
            except subprocess.TimeoutExpired:
                return "Error: tests timed out after 120 seconds."
        return (result.stdout + result.stderr)[-4000:] or "no output"
    if name == "list_files":
        return "\n".join(sorted(p.name for p in root.iterdir() if p.is_file()))
    if name not in ("read_file", "write_file"):
        return f"Error: unknown tool {name}."
    if not isinstance(args.get("path"), str):
        return "Error: missing path."
    path = (root / args["path"]).resolve()
    if path.parent != root:
        return "Error: path is outside the project."
    if name == "read_file":
        return path.read_text() if path.is_file() else "Error: file not found."
    if path.name.startswith("test_") or path.name == "TASK.md":
        return "Error: task and test files are read-only."
    if not isinstance(args.get("content"), str):
        return "Error: missing content."
    path.write_text(args["content"])
    return f"Wrote {len(args['content'])} characters to {path.name}."


class Gateway:
    def __init__(self, url: str, key: str, timeout: int = 180) -> None:
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def request(self, method: str, path: str, session: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.url + path, data, method=method,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "x-request-id": session})
        with open_url(request, timeout=self.timeout) as response:
            payload = json.load(response)
            if isinstance(payload, dict):
                payload["_selected_model"] = response.headers.get("x-litellm-model-name")
            return payload

    def model_groups(self) -> list[str]:
        """Model groups the gateway serves, with the routed group first."""
        return model_groups(lambda: self.request("GET", "/v1/models", f"models-{uuid.uuid4().hex[:8]}"), self.url)


def model_groups(fetch: Callable[[], object], url: str) -> list[str]:
    """Model group ids from GET /v1/models, routed group first; exit with a clear message on failure."""
    where = f"{url.rstrip('/')}/v1/models"
    try:
        payload = fetch()
    except (urllib.error.URLError, TimeoutError, ValueError, http.client.HTTPException, OSError) as exc:
        sys.exit(f"Could not list model groups at {where}: {exc}")
    data = payload.get("data") if isinstance(payload, dict) else None
    entries = data if isinstance(data, list) else []
    ids = sorted({e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)})
    if not ids:
        sys.exit(f"Could not list model groups at {where}: the gateway lists none (check the routing profile)")
    return sorted(ids, key=lambda group: group != ROUTED_GROUP)


def parse_tool_calls(calls: object) -> list[tuple[str, str, dict]]:
    """(id, name, arguments) for each tool call; ValueError when any call is malformed.

    Arguments may arrive as a JSON string or as an already-parsed object."""
    if not isinstance(calls, list):
        raise ValueError(f"tool_calls is not a list: {str(calls)[:200]}")
    parsed = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise ValueError(f"no function in {str(call)[:200]}")
        name, call_id = function.get("name"), call.get("id")
        if not isinstance(name, str) or not name:
            raise ValueError(f"no function name in {str(call)[:200]}")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError(f"no id for {name}")
        arguments = function.get("arguments")
        if arguments is None or arguments == "":
            arguments = {}
        elif isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON arguments for {name}: {exc}") from exc
        if not isinstance(arguments, dict):
            raise ValueError(f"arguments for {name} must be an object")
        parsed.append((call_id, name, arguments))
    return parsed


def tests_pass(output: str) -> bool:
    lines = output.rstrip().splitlines()
    return bool(lines and lines[-1].startswith("OK"))


def solve(gateway: Gateway, route: str, task: str, out_dir: Path | None = None,
          max_turns: int = MAX_TURNS, max_tokens: int = 4096) -> dict:
    source = HERE / "tasks" / task
    if source.parent != HERE / "tasks" or not (source / "TASK.md").is_file():
        raise ValueError(f"Unknown task: {task}")
    session = f"{task}-{uuid.uuid4().hex[:8]}"
    artifact = (out_dir or HERE / "results") / session
    original, root = artifact / "original", (artifact / "workspace").resolve()
    shutil.copytree(source, original)
    shutil.copytree(source, root)
    task_text = (root / "TASK.md").read_text()
    print(f"\nTask: {task}\n{task_text.strip()}\nModel group: {route}\n", flush=True)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task_text}]
    turns, error = [], None
    tests_complete = False
    started = time.monotonic()
    transcript = artifact / "transcript.jsonl"

    def record(event: dict) -> None:
        with transcript.open("a") as handle:
            handle.write(json.dumps(event) + "\n")

    record({"event": "task", "task": task, "model_group": route, "description": task_text})
    for number in range(1, max_turns + 1):
        try:
            data = gateway.request("POST", "/v1/chat/completions", session,
                {"model": route, "messages": messages, "tools": TOOLS, "max_tokens": max_tokens})
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError(f"unexpected message {str(message)[:200]}")
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError, OSError) as exc:
            error = f"Request failed: {exc}"
            record({"event": "error", "message": error})
            break
        usage = data.get("usage") or {}
        selected = data.get("_selected_model")
        turn = {"model": selected, "usage": usage, "estimated_cost": usage_cost(selected, usage),
                "unpriced_reason": unpriced_reason(selected, usage)}
        turns.append(turn)
        print(f"Turn {number}: {selected or 'selected model unavailable'}", flush=True)
        record({"event": "assistant", "turn": number, **turn, "message": message})
        messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls") and v is not None})
        calls = message.get("tool_calls") or []
        if not calls:
            if message.get("content"):
                print(message["content"], flush=True)
            break
        try:
            parsed = parse_tool_calls(calls)
        except ValueError as exc:
            error = f"Malformed tool call: {exc}"
            record({"event": "error", "message": error})
            break
        for call_id, name, arguments in parsed:
            try:
                output = run_tool(root, name, arguments)
            except OSError as exc:
                output = f"Error: {exc}"
            if name == "write_file":
                tests_complete = False
            elif name == "run_tests":
                tests_complete = tests_pass(output)
            print(f"  {name} {arguments.get('path', '')}", flush=True)
            if name == "run_tests":
                print(output, flush=True)
            record({"event": "tool", "name": name, "arguments": arguments, "output": output})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
        if tests_complete:
            break

    test_output = run_tool(root, "run_tests", {})
    (artifact / "tests.txt").write_text(test_output)
    patch = ""
    for file in sorted(root.iterdir()):
        if not file.is_file() or file.name.startswith("test_") or file.name == "TASK.md":
            continue
        before = original / file.name
        patch += "".join(difflib.unified_diff(
            before.read_text().splitlines(keepends=True) if before.exists() else [],
            file.read_text().splitlines(keepends=True),
            fromfile="original/" + file.name, tofile="workspace/" + file.name))
    (artifact / "changes.patch").write_text(patch)
    estimates = [turn["estimated_cost"] for turn in turns]
    if not turns:
        estimated = 0.0  # failed before the first turn: no response, nothing to price
    else:
        estimated = sum(estimates) if all(value is not None for value in estimates) else None
    reasons = [turn["unpriced_reason"] for turn in turns]
    unpriced = sorted({turn["model"] for turn in turns if turn["unpriced_reason"] == "price"})
    result = {"task": task, "model_group": route, "route": route, "session": session,
              "passed": tests_pass(test_output), "error": error, "turns": len(turns),
              "models": sorted({turn["model"] for turn in turns if turn["model"]}),
              "estimated_cost": estimated, "unpriced_models": unpriced,
              "cached_turns": reasons.count("cached"), "unavailable_turns": reasons.count("unavailable"),
              "seconds": round(time.monotonic() - started, 1),
              "artifacts": str(artifact)}
    (artifact / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    record({"event": "result", **result})
    print(f"\nTests: {'PASS' if result['passed'] else 'FAIL'}\n{patch or 'No source changes.'}", flush=True)
    print(f"Saved {artifact}", flush=True)
    return result


def summary_rows(results: list[dict]) -> list[dict]:
    """Per model group: runs, solved, errors, and mean cost per task and per solved task (None when unpriced)."""
    by_route: dict[str, list[dict]] = {}
    for result in results:
        by_route.setdefault(result["route"], []).append(result)
    rows = []
    for route, runs in by_route.items():
        solved = sum(1 for run in runs if run["passed"])
        costs = [run["estimated_cost"] for run in runs]
        harness = sum(1 for run in runs if str(run.get("error") or "").startswith("harness"))
        before_first = sum(1 for run in runs if run.get("error") and not run.get("turns")) - harness
        notes = []
        if any(run.get("unpriced_models") for run in runs):
            notes.append("add prices to PRICES")
        if any(run.get("cached_turns") for run in runs):
            notes.append("cached input is not priced")
        if any(run.get("unavailable_turns") for run in runs):
            notes.append("selected model or usage unavailable")
        if harness:
            notes.append(f"{harness} harness error{'s' if harness > 1 else ''}, see output")
        if before_first:
            notes.append(f"{before_first} run{'s' if before_first > 1 else ''} failed before the first turn "
                         "(counted as $0)")
        row = {"route": route, "runs": len(runs), "solved": solved,
               "errors": sum(1 for run in runs if run.get("error")),
               "cost_per_task": None, "cost_per_solved": None, "note": "; ".join(notes)}
        if all(c is not None for c in costs):
            total = sum(costs)
            row["cost_per_task"] = total / len(runs)
            row["cost_per_solved"] = total / solved if solved else None
        rows.append(row)
    return rows


def summarize(results: list[dict]) -> None:
    print("\nSummary (USD estimates from per-turn token usage and the model that answered; "
          "turns with cached input are left unpriced)")
    print(f"{'ROUTE':24s} {'RUNS':>4s} {'SOLVED':>6s} {'ERRORS':>6s} {'COST/TASK':>10s} {'COST/SOLVED':>12s}")
    for row in summary_rows(results):
        line = f"{row['route']:24s} {row['runs']:4d} {row['solved']:6d} {row['errors']:6d}"
        if row["cost_per_task"] is None:
            print(f"{line} {'n/a':>10s} {'n/a':>12s} ({row['note'] or 'unpriced'})")
            continue
        per_solved = "-" if row["cost_per_solved"] is None else f"{row['cost_per_solved']:.6f}"
        line = f"{line} {row['cost_per_task']:10.6f} {per_solved:>12s}"
        print(line + (f" ({row['note']})" if row["note"] else ""))


def run_all(gateway: Gateway, routes: list[str], tasks: list[str], repeats: int, out_dir: Path,
            max_turns: int = MAX_TURNS, max_tokens: int = 4096) -> tuple[list[dict], Path]:
    """Run every task on every route, appending one summary record per run to a JSONL file in out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = out_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.jsonl"
    results = []
    for _ in range(repeats):
        for route in routes:
            for task in tasks:
                try:
                    result = solve(gateway, route, task, out_dir, max_turns, max_tokens)
                except Exception:  # keep one unexpected failure from ending the comparison
                    error = "harness: " + traceback.format_exc()[-600:]
                    print(error, file=sys.stderr, flush=True)
                    result = {"task": task, "model_group": route, "route": route, "session": None,
                              "passed": False, "error": error, "turns": 0, "models": [],
                              "estimated_cost": None, "unpriced_models": [], "cached_turns": 0,
                              "unavailable_turns": 0, "seconds": 0.0, "artifacts": None}
                results.append(result)
                with summary.open("a") as handle:
                    handle.write(json.dumps(result) + "\n")
                estimate = result["estimated_cost"]
                print("Estimated token cost: " + ("unavailable" if estimate is None else f"${estimate:.6f}"),
                      flush=True)
    return results, summary


def task_names() -> list[str]:
    return sorted(p.name for p in (HERE / "tasks").iterdir() if (p / "TASK.md").is_file())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-tasks", action="store_true")
    parser.add_argument("--tasks", nargs="+", choices=task_names(), default=["slugify"])
    routes = parser.add_mutually_exclusive_group()
    routes.add_argument("--routes", nargs="+",
                        help="model groups to compare (default: every group from GET /v1/models, switchyard first)")
    routes.add_argument("--model", help="one model group; same as --routes MODEL")
    parser.add_argument("--repeats", type=int, default=1, help="runs of each task on each model group")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--out-dir", type=Path, default=HERE / "results")
    args = parser.parse_args(argv)
    if min(args.repeats, args.max_turns, args.max_tokens) < 1:
        parser.error("repeats, max-turns, and max-tokens must be positive")
    return args


def resolve_routes(args: argparse.Namespace, gateway) -> list[str]:
    if args.model:
        return [args.model]
    return args.routes or gateway.model_groups()


def main() -> None:
    args = parse_args()
    if args.list_tasks:
        width = max(20, min(88, shutil.get_terminal_size().columns))
        for task in task_names():
            description = (HERE / "tasks" / task / "TASK.md").read_text().strip()
            print(f"{task}:")
            print(textwrap.fill(description, width=width, initial_indent="  ", subsequent_indent="  "))
            print()
        return
    url, key = os.environ.get("GATEWAY_URL"), os.environ.get("LITELLM_MASTER_KEY")
    if not url or not key:
        sys.exit("Set GATEWAY_URL and LITELLM_MASTER_KEY from the stack outputs")
    gateway = Gateway(url, key)
    routes = resolve_routes(args, gateway)
    results, summary = run_all(gateway, routes, args.tasks, args.repeats, args.out_dir,
                               args.max_turns, args.max_tokens)
    summarize(results)
    print(f"\nSaved {summary}")
    sys.exit(1 if any(r["error"] or not r["passed"] for r in results) else 0)


if __name__ == "__main__":
    main()
