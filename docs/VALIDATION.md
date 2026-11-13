# Validation record: LiteLLM plugin migration

Checked October 21, 2026.

## Build and local checks

These counts are from the migration commit. The [Hardening changes](#hardening-changes) section records the current counts.

- ARM64 gateway image built successfully.
- Switchyard source: `21075c6652152dac88a5fbc6b878ca37d7617c1c`.
- LiteLLM: 1.102.0.
- TypeScript build and ESLint/Prettier checks passed.
- 84 TypeScript unit and stack tests passed, including the migration log-retention check.
- Five Python runner tests passed.
- Three offline proxy tests passed, covering authentication, Stage selection and escalation, Random candidate boundaries, and saved coding-task artifacts.
- Both example profiles passed configuration validation.
- The npm dependency audit reported no vulnerabilities.

### Hardening checks (local image)

Checked October 21, 2026, with the LiteLLM 1.102.0 gateway image and the Stage profile set to `mock_response`, so no provider was called. The container published no port. Requests went from inside the container to `127.0.0.1:4000` without a key, except the chat completion. "Flags" means `DISABLE_ADMIN_UI`, `NO_DOCS`, `NO_REDOC`, and `NO_OPENAPI` set to `True`.

| Path                         | Without flags          | With flags                    |
| ---------------------------- | ---------------------- | ----------------------------- |
| `/`                          | 200, Swagger UI        | 200, `"LiteLLM: RUNNING"`     |
| `/redoc`                     | 200, ReDoc             | 404                           |
| `/openapi.json`              | 200, OpenAPI document  | 404                           |
| `/docs`                      | 404                    | 404                           |
| `/sso/key/generate`          | 200, setup page        | 200, "Admin UI Disabled" page |
| `/ui`, `/ui/login`           | 200, LiteLLM Dashboard | 200, LiteLLM Dashboard        |
| `/health/liveliness`         | 200                    | 200                           |
| `/v1/chat/completions` (key) | 200, mock reply        | 200, mock reply               |

`/ui` still returns 200 locally: LiteLLM 1.102.0 serves the `/ui` static files even with `DISABLE_ADMIN_UI` set. In AWS, the load balancer forwards only `/v1/chat/completions`, `/v1/models`, and `/health/liveliness` and answers every other path, including `/ui`, with its own 404. Locally, Compose publishes the port on `127.0.0.1` only. Stack tests check the load balancer rules; the 404 responses have not been checked against a deployed stack in this run. The smoke test checks them on its next run against a deployed gateway.

## Real Bedrock checks

The pinned image ran locally with temporary AWS credentials in `us-east-1`:

- Stage ordinary request selected Nemotron 3 Super 120B.
- Stage repeated-failure fixture selected Claude Opus 5.5.
- Random requests stayed inside the configured two-model pool; both candidates were observed.
- The live slugify coding task passed all four supplied tests within the six-turn limit. It saved original and final files, a transcript, test output, a patch, and a result.

These checks validate the provider integration through the local container. They are not a benchmark comparison. The local coding artifacts are gitignored workload data.

## AWS deployment review

The migration diff uses the existing stack's allowed CIDR. Network, load-balancer, and secret logical resources are preserved. Internal security-group rules switch from port 8080 to 4000. The first migration uses `retainLegacyLogs=true` to retain the previous Switchyard log group.

The existing `SwitchyardGateway` stack was updated successfully in `us-east-1` using the Stage profile. CloudFormation reported `UPDATE_COMPLETE`; ECS reported a completed deployment with one desired and one running ARM64 task containing only the `litellm` container.

The deployed endpoint passed health, missing-key rejection, model-group discovery, efficient selection, and controlled escalation checks. The live slugify task then passed all four supplied tests in five model turns, selecting Nemotron 3 Super 120B throughout, and saved its workload artifacts under the gitignored `bench/results/` directory.

The existing gateway URL and master-key secret were preserved. Random was exercised through the local image against real Bedrock, rather than deployed as a separate ECS stack.

## Additional coding-task runs

The saved task artifacts from October 21, 2026 also record these runs through the deployed Stage profile:

| Task    | Model calls | Supplied tests | Models selected                      |
| ------- | ----------: | -------------: | ------------------------------------ |
| Roman   |           5 |       4 passed | Nemotron 3 Super                     |
| LRU/TTL |           4 |      10 passed | Nemotron 3 Super                     |
| Calc    |          12 |       7 passed | Nemotron 3 Super and Claude Opus 5.5 |

The Calc run recorded failed test calls on turns 5, 7, and 9, selected Opus for a file read on turn 10, and returned to Super for the final edit and passing test call. Opus's response contained no visible diagnosis. This trace establishes that both models were selected during the task; it does not establish that the Opus call caused the successful repair.

A separate Calc run limited to four model calls failed its final test check with a syntax error. That check happened after the model loop ended, so its result was not supplied to another routing request. These are individual observations, not repeatability or savings measurements.

Raw transcripts and generated workspaces remain under the gitignored `bench/results/` directory. The README contains a summary without local paths, credentials, or raw workload files.

## Hardening changes

Checked October 21, 2026. After the migration, three hardening changes were verified locally against the pinned gateway image: the load balancer path allowlist with LiteLLM's docs routes turned off, the Converse pin for every model with the bundled cost map, and the single-model baseline groups behind the `switchyard_gateway` wrapper.

### Offline checks

All on ARM64 macOS with Docker (Colima); no provider was called.

- `npm ci`, ESLint/Prettier, and the TypeScript build passed.
- 109 TypeScript unit and stack tests passed in six suites.
- 96 Python runner tests passed (`test/python`).
- Every Python file under `bench`, `scripts`, and `test` compiled, and shellcheck reported no findings for `scripts/*.sh`.
- The gateway image was rebuilt, and both example profiles passed configuration validation.
- Eight in-image routing tests passed with networking disabled: profile loading, Stage selection and escalation, Random pool and weights, baseline pass-through, and Stage rejecting a baseline group without the wrapper.
- Five offline proxy tests passed against a mock provider: authentication, Stage selection and escalation, saved coding-task artifacts, baseline pass-through, the Random pool, `/v1/models` listing, and unpriced turns with missing usage.
- Both profiles synthesized with no AWS credentials, using a placeholder account and a seeded Availability Zone context.

### Real Bedrock checks

Both profiles ran through Compose in `us-east-1` with temporary AWS credentials and a fresh master key. The published port listened on `127.0.0.1:4000` only. The smoke test passed for each profile; it skips the load balancer checks for a local gateway.

| Profile | Request                          | Selected model                 |
| ------- | -------------------------------- | ------------------------------ |
| Stage   | Ordinary request                 | Nemotron 3 Super 120B          |
| Stage   | Repeated-failure fixture         | Claude Opus 5.5                |
| Random  | Four ordinary requests           | Opus, Nemotron, Opus, Nemotron |
| Both    | `claude-opus-5-5` baseline       | Claude Opus 5.5                |
| Both    | `nemotron-super-3-120b` baseline | Nemotron 3 Super 120B          |

With the Stage profile running, one slugify run per model group passed:

| Route                   | Model calls | Supplied tests | Models selected  | Estimated cost (USD) |
| ----------------------- | ----------: | -------------- | ---------------- | -------------------: |
| `switchyard`            |           5 | Passed         | Nemotron 3 Super |             0.000792 |
| `claude-opus-5-5`       |           2 | Passed         | Claude Opus 5.5  |             0.012756 |
| `nemotron-super-3-120b` |           5 | Passed         | Nemotron 3 Super |             0.000823 |

One multiturn run (slugify coding task and the algorithms chat script, capped at $0.50) also passed on every route:

| Route                   | Coding        | Chat checks | Models selected  | Total estimated cost (USD) |
| ----------------------- | ------------- | ----------- | ---------------- | -------------------------: |
| `switchyard`            | 1/1 (3 calls) | 5/5         | Nemotron 3 Super |                   0.002498 |
| `claude-opus-5-5`       | 1/1 (1 call)  | 5/5         | Claude Opus 5.5  |                   0.077908 |
| `nemotron-super-3-120b` | 1/1 (3 calls) | 5/5         | Nemotron 3 Super |                   0.001482 |

These are single observations, not a repeatability or savings measurement. Costs are estimates from per-turn token usage.

### Deployed check (October 22, 2026)

- The Linux CI workflow passed on the pull request: lint, type-check, unit and stack tests, synth of both profiles, Python checks, the image build, the in-image routing tests, the offline proxy tests, and the secret scan.
- The existing `SwitchyardGateway` stack in `us-east-1` was updated in place to the Stage profile with `retainLegacyLogs=true`. CloudFormation completed the update; ECS reported one running ARM64 task and a completed rollout. The gateway URL and master-key secret were preserved.
- The smoke test passed through the load balancer, reading the URL and key from the stack outputs. A plain request selected `bedrock/converse/nvidia.nemotron-super-3-120b`, the repeated-failure fixture selected `bedrock/converse/global.anthropic.claude-opus-5-5`, each baseline group answered with its own model, and every closed path returned the load balancer's `Not found`.
- Direct requests to the load balancer: `/health/liveliness` returned 200; `/ui`, `/docs`, `/v1/models/`, `/chat/completions`, and `/key/generate` returned 404; a chat request without a key returned 401.

- The stack was then updated to the Random profile. The smoke test passed through the load balancer: `switchyard` picks included both Nemotron 3 Super and Claude Opus 5.5, each baseline answered with its own model, and every closed path returned `Not found`.
- The README key rotation steps ran as written in 2 min 50 s, including the wait for the ECS service to stabilise. Afterwards `/v1/models` rejected the old key with HTTP 400 and accepted the new one with HTTP 200.

### Not verified

- HTTPS (custom domain) was not deployed.

## Request-flow check (October 28, 2026)

A fresh `SwitchyardGateway` stack (Stage, ARM64, HTTP, one `/32` allowed CIDR) was deployed to `us-east-1` and checked against each step of the README's request flow:

| Step | Check                                                                                                                                                                                                                            |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | `"model": "switchyard"` with the gateway key returned 200; without the key, 401. The load balancer security group allowed only port 80 from the configured `/32`.                                                                |
| 2    | The web ACL on the load balancer had the IP reputation, known-bad-inputs, and per-IP rate-limit rules. The smoke test's closed paths answered 404. The task ran on Fargate in a private subnet with no public IP and NAT egress. |
| 3    | The task ran one container, `litellm`. A plain request selected Nemotron 3 Super 120B; the escalation fixture selected Claude Opus 5.5. Baseline groups each answered with their own model.                                      |
| 4    | CloudTrail recorded `Converse` and `ConverseStream` calls from the task role, each through the Bedrock Runtime interface endpoint (`vpcEndpointId` set). The task role's Bedrock grant named the two configured models.          |
| 5    | Non-streaming and streaming responses carried `x-litellm-model-name`; the streamed response ended with `[DONE]`.                                                                                                                 |

HTTPS was not deployed in this check.

## Image update check (October 29, 2026)

The gateway image was rebuilt for ARM64 on LiteLLM 1.102.2 with the patched Python and Wolfi packages and without Node.js and ddtrace. No provider was called.

- The build's `uv pip check` step reported only the base image's own `langfuse`/`packaging` conflict, and the image has no `node` binary.
- Eight in-image routing tests passed with networking disabled, and the five offline proxy tests passed against the mock provider.
- The keyless path checks from the [hardening checks](#hardening-checks-local-image) table, repeated with the flags set, gave the same results on 1.102.2: `/ui` and `/ui/login` still return 200, so the load balancer allowlist is still what blocks them in AWS.
- LiteLLM 1.102.2 still sends `bedrock/global.anthropic.claude-opus-5-5` to InvokeModel and `bedrock/converse/global.anthropic.claude-opus-5-5` to Converse.

The x86_64 image was not built locally; the CI image job builds it on Linux. No stack was deployed in this check.

## Coverage limits

The image was built and exercised on ARM64. The x86_64 stack configuration was synthesized in tests, but an x86_64 image was not built in this run. No broader model matrix, HTTPS deployment, or cost-saving benchmark was run.
