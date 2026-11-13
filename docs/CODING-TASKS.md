# Coding tasks

The demo runs a small coding agent through the gateway. Each task has a description, starter code, and read-only tests.

| Task      | What the agent changes                                                      | Contract                                                                                                                          |
| --------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `slugify` | Fixes lowercase text, punctuation removal, and word separators.             | [Task](../bench/tasks/slugify/TASK.md), [starter](../bench/tasks/slugify/slug.py), [tests](../bench/tasks/slugify/test_slug.py)   |
| `roman`   | Implements integer-to-Roman-numeral conversion for 1 through 3999.          | [Task](../bench/tasks/roman/TASK.md), [starter](../bench/tasks/roman/roman.py), [tests](../bench/tasks/roman/test_roman.py)       |
| `calc`    | Implements a safe expression parser with precedence, functions, and errors. | [Task](../bench/tasks/calc/TASK.md), [starter](../bench/tasks/calc/calc.py), [tests](../bench/tasks/calc/test_calc.py)            |
| `lru_ttl` | Implements a thread-safe cache with eviction, expiration, and statistics.   | [Task](../bench/tasks/lru_ttl/TASK.md), [starter](../bench/tasks/lru_ttl/cache.py), [tests](../bench/tasks/lru_ttl/test_cache.py) |

List the tasks without making model calls:

```bash
python3 bench/run.py --list-tasks
```

Start with slugify after exporting the gateway URL and key:

```bash
python3 bench/run.py --tasks slugify --model switchyard --max-turns 6
```

The agent can list and read files, replace source files, and run the supplied tests. The terminal shows the task, selected model for each turn, tool actions, test output, and final diff. A task can finish successfully, fail its tests, hit its turn limit, or encounter a provider error.

For the longer parser task, use `--tasks calc --max-turns 12`. Each model call counts as a turn, including calls that only read a file. Leave enough turns for the agent to run tests, receive the results, and attempt repairs. A failure discovered by the runner's final test check after the loop ends is saved locally but does not trigger another gateway request.

Each run saves a directory under `bench/results/` containing:

- `original/`: the task's starting files.
- `workspace/`: the files after the run.
- `transcript.jsonl`: model messages, tool calls, tool results, and the outcome.
- `changes.patch`: the source changes.
- `tests.txt`: the final test output.
- `result.json`: models used, outcome, usage-based cost estimate where available, and elapsed time.

## Compare model groups

Each profile serves the routed `switchyard` group and one single-model baseline group per candidate (`claude-opus-5-5`, `nemotron-super-3-120b`) in the same deployment. Without `--model` or `--routes`, the runner calls every group from `GET /v1/models`, `switchyard` first, and exits with a message if that list fails or is empty:

```bash
python3 bench/run.py --tasks slugify roman --repeats 3
```

The summary shows `RUNS`, `SOLVED`, `ERRORS`, `COST/TASK`, and `COST/SOLVED` (USD, six decimals) for each group. Each turn is priced from its `x-litellm-model-name` header: an exact `PRICES` alias first, then the longest Bedrock model ID in the header that ends the string or is followed by `:` or `-v<digit>`. A group shows `n/a` with the reason when any of its runs has an unpriced turn (unknown model, cached input, or a response without the selected-model header or token counts) or an unexpected harness error. A run that failed before its first turn counts as $0, with a note. A malformed tool call (no function, name, or id, or arguments that aren't a JSON object) ends that run with an error, and an unexpected exception becomes a failed result record; either way the remaining runs and the summary still run. Add a `PRICES` entry when you add a model to a profile. Besides the per-run directories, `bench/results/run-<timestamp>.jsonl` holds one summary record per run.

`--model <group>` is the same as `--routes <group>`.

## Multi-turn conversations

`bench/multiturn.py` compares whole conversations instead of single task runs. It uses the same tasks and `PRICES`, and by default the same model groups:

```bash
python3 bench/multiturn.py --tasks slugify --scripts backend api --max-spend 2
```

- Coding conversations include the project files in the first prompt and feed the test output back after every edit, up to `--max-turns` model turns.
- Chat scripts (`algorithms`, `backend`, `concurrency`, `api`) replay the full history on every turn. Checks cover expected text, word limits, bullet lists, and assertions run against returned code in a credential-stripped subprocess.
- `--max-conv-cost` caps each conversation (default 1 USD; `0` removes the cap). `--max-spend` stops starting new conversations. With either budget set, an unpriced turn (unknown model or cached input) stops its conversation and the run, because its spend can't be counted against the budget.
- A harness error is recorded and the run continues. A 200 response without a reply message, or a malformed tool call (parsed the same way as in `bench/run.py`), counts as a turn error and ends that conversation; earlier turns keep their cost.

The JSONL output has one `turn` record per model call and one `conversation` record per conversation. The summary shows, per model group, conversations, coding passes, chat checks, and estimated USD per coding conversation, per chat, and in total.

## Cost and safety

These requests incur Bedrock charges. The example limits turns and output tokens; those limits aren't a dollar budget. Costs are estimates from recorded successful responses. Unknown models, missing usage, and cached input are left unpriced. Provider retries can also incur charges that aren't represented by the final response.

Run model-written code in a disposable development environment. The runner removes credentials from the test subprocess environment and runs the supplied tests on a temporary copy of the workspace, with `HOME` set to that copy, but it is not an operating-system sandbox. Files that the tests write stay in the copy and are deleted with it, so the saved workspace has only the model's edits. Chat checks in `bench/multiturn.py` also run with `HOME` set to their own temporary directory.

The Stage smoke test uses a supplied failure history to check escalation. A coding task uses live tool results and can finish without triggering escalation. One run demonstrates the workflow; it does not establish a cost or quality advantage.
