# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Changed

- `assets/architecture.png` is exported at 2x scale from a new editable source,
  `assets/architecture.drawio`, which replaces the outdated source that still showed the
  earlier model alternatives. The diagram shows the single-container gateway with both example
  profiles, numbered to match the README's five-step request flow.
- `docs/TROUBLESHOOTING.md` explains the HTTP 400 `no_db_connection` response that a wrong
  `sk-` key gets from the gateway.
- `scripts/smoke-test.sh` uses an exported `LITELLM_MASTER_KEY` only when `GATEWAY_URL` is also
  set; otherwise it reads both the URL and the key from the stack outputs.
- **Breaking:** The gateway runs one LiteLLM container with NVIDIA's Switchyard LiteLLM
  routing plugin (Switchyard source `21075c6`, LiteLLM 1.102.2) in place of the standalone
  `switchyard-server` in front of LiteLLM. See `docs/PLUGIN-MIGRATION.md`.
  - **Breaking:** Clients send `"model": "switchyard"` or a baseline group name instead of a
    route ID such as `task`, `execution`, or `auto`.
  - **Breaking:** The selected model is in the `x-litellm-model-name` response header, which
    carries the full LiteLLM model string. `x-model-router-selected-model` is gone.
  - **Breaking:** The load balancer targets LiteLLM on port 4000 instead of the server on
    port 8080.
- Configuration lives in deployment profiles, `profiles/<name>/litellm.yaml` and
  `switchyard.toml`, with Stage and Random examples that share Claude Opus 5.5 and
  Nemotron 3 Super 120B. Select one with `-c routingProfile=<name>` or `ROUTING_PROFILE`;
  the stack reports it in the `RoutingProfile` output.
- The stack validates the selected profile and derives Bedrock invocation permissions from
  its models. `npm run validate:profile -- <name>` runs the same checks.
- The coding demo and benches record usage and the selected model from each response.
  Coding-task workspaces, transcripts, diffs, and results are saved under `bench/results/`.
- The load balancer forwards only `/v1/chat/completions`, `/v1/models`, and
  `/health/liveliness`; every other path gets the load balancer's own 404. The smoke test
  checks that the closed paths answer that 404.
- LiteLLM's Swagger, ReDoc, and OpenAPI routes are off (`NO_DOCS`, `NO_REDOC`, `NO_OPENAPI`)
  and `DISABLE_ADMIN_UI` is set. LiteLLM 1.102.2 still serves the `/ui` static files; the
  load balancer allowlist blocks `/ui` in AWS, and Compose publishes on 127.0.0.1 only.
- Every model goes through the Bedrock Converse API (`bedrock/converse/<id>`), and LiteLLM
  uses the cost map bundled in the pinned image (`LITELLM_LOCAL_MODEL_COST_MAP=True`, set in
  the image, the ECS task definition, and Compose).
- Validation requires that only `switchyard` has more than one deployment, that every routed
  model has a baseline group, that every model uses `bedrock/converse/`, and that the
  profile registers only the gateway wrapper as its plugin.
- `bench/run.py` compares cost per task across model groups: `--routes` (default: every
  group from `/v1/models`, `switchyard` first) and `--repeats`, a summary with `RUNS`,
  `SOLVED`, `ERRORS`, `COST/TASK`, and `COST/SOLVED` columns, and one JSONL summary record
  per run.
  Turns are priced by Bedrock model ID from the selected-model header; cached input stays
  unpriced.
- `bench/multiturn.py` works with the plugin gateway: it defaults to every model group, stops
  on unpriced spend only when a budget is set (`--max-conv-cost 0` removes the
  per-conversation cap), records harness errors without stopping the run, treats 200
  responses without a reply as turn errors, and prints a summary per model group.
- The gateway image mounts `uv` only for the wheel install, so `uv` is not in the runtime
  image. `start.sh` reports a missing or unreadable configuration file by path.
- The gateway image is built on LiteLLM 1.102.2 and pins every base image by digest. The Rust
  builder comes from the Docker Official Image mirror on Amazon ECR Public, and uv comes from
  PyPI with hashes instead of the `ghcr.io/astral-sh/uv` image. The image no longer contains
  Node.js or the Datadog tracer, so LiteLLM's database (Prisma) features and Datadog tracing
  aren't available. The README's "Bumping pinned images and packages" section explains each pin.
- Dependabot proposes updates to the gateway image's Python requirements files.
- CI synthesizes both profiles, runs the in-image routing tests, sets up Node.js in every job
  that runs npm, and has timeouts on the image job and the offline proxy tests.
- CI builds the gateway image from a copy of the Dockerfile that pulls the Docker Official
  Images from Docker Hub by the same digests, because anonymous Amazon ECR Public pulls from
  shared runners hit its data limit. The Dockerfile still pulls from Amazon ECR Public.
- Dependabot watches `docker/gateway`. The pull request and bug report templates name the
  profile commands.

### Added

- One single-model baseline group per routed model in each profile (`claude-opus-5-5`,
  `nemotron-super-3-120b`), and the `switchyard_gateway` wrapper in the image, which applies
  Switchyard to groups with two or more unique candidates and passes single-model groups
  through.
- In-image routing tests under `docker/gateway/tests`, offline proxy tests with a mock
  provider (`npm run test:proxy`), and baseline checks in the smoke test.
- `ClusterName` and `ServiceName` stack outputs, used by the README's key rotation steps.
- `E2E_PROVIDER_HOST` and `E2E_READY_TIMEOUT` (default 180 seconds) for the offline proxy
  tests.
- `THIRD-PARTY-LICENSES`, which lists the third-party software the build pulls in, including
  the LiteLLM image's `enterprise/` directory and its separate license.
- A README "Before production" list (HTTPS, per-user keys with budgets, customer managed KMS
  keys, ALB access logs and VPC flow logs, container hardening, a request-size limit), and a
  "Known limitations" section in `SECURITY.md` for the items the sample accepts.

### Fixed

- Bench comparisons keep running past bad responses: malformed tool calls end only their run,
  unexpected exceptions become failed records, runs that fail before the first turn count as
  $0, missing headers or usage get their own unpriced reason, an unpriced turn stops a
  multi-turn conversation under either budget, queued conversations are cancelled once a run
  stops, and summaries show six decimals.
- The offline proxy tests no longer assume rootless Docker reaches the host's loopback; they
  ask for `E2E_PROVIDER_HOST` instead.

### Removed

- **Breaking:** The standalone `switchyard-server` container, its route catalog (`auto`,
  `task`, `execution`, `composite`, `random-oss`, and the per-model passthroughs), and its
  session-statistics endpoints. Task and Composite routing need judge calls that the plugin
  integration does not make.
- `config/litellm.yaml`, `config/switchyard.toml`, and `docker/switchyard-server/`.
- The `/SwitchyardGateway/switchyard` log group. Deploy the migration once with
  `-c retainLegacyLogs=true` to keep it.

### Security

- **Breaking:** The gateway serves HTTPS by default. Synth fails unless you pass `domainName`
  with `hostedZone` or with the new `certificateArn` (an existing ACM certificate in the stack's
  Region; the stack creates no DNS record and outputs `LoadBalancerDnsName` for your CNAME), or
  opt in to plain HTTP with `-c allowPlainHttp=true`. Ambiguous combinations fail. **Upgrade
  note:** a stack deployed over HTTP needs `-c allowPlainHttp=true` on its next `cdk diff` and
  `cdk deploy`, or the HTTPS settings to move to HTTPS.
- With the plain-HTTP opt-in, CDK prints a synth warning (`switchyard-gateway:plain-http-listener`)
  that names what crosses the network unencrypted, and the `GatewayUrl` output description says
  the URL is for short tests only. The README and `SECURITY.md` say the same, and recommend an
  AWS Budgets alert for Amazon Bedrock, because every key holder can spend without a cap.
- The load balancer drops header fields whose names are not letters, digits, and hyphens
  (`routing.http.drop_invalid_header_fields.enabled`).
- AWS WAF no longer keeps sampled requests, which stored every request header, including the
  master key, for three hours. CloudWatch metrics stay on.
- The cdk-nag acknowledgment for the Bedrock foundation-model wildcard matches the partition
  that `cdk synth` renders, and a new test runs cdk-nag on the app with the `cdk.json` context.
  The acknowledgment reason now says that the wildcard matches every model ID with the same
  prefix and that the grant isn't conditioned on the inference profile ARN. IAM permissions are
  unchanged.
- CI pins actions by commit SHA, doesn't persist the checkout token, and scans the history with
  the gitleaks 8.30.1 CLI (checksum-verified, `--redact`) instead of gitleaks-action, which
  needs a license in an organization. shellcheck also covers `docker/gateway/start.sh`.
- `aws-cdk-lib` 2.274.0 bundles `brace-expansion` 5.0.12, so `npm ci` reports no high-severity
  advisory and the postinstall patch has nothing left to remove.
- The gateway image raises the minimum version of `glibc-2.44` and its subpackages
  (`glibc-2.44-locale-posix`, `ld-linux-2.44`, `libcrypt1-2.44`) to 2.44-r4 for CVE-2026-19499,
  and of `zlib` to 1.3.2.1_rc20260601-r0 for CVE-2026-85091.
- The image build moves the Rust crates in Switchyard's `Cargo.lock` that have published
  advisories to their first fixed releases before the `--locked` build: rustls 0.23.46,
  rustls-webpki 0.103.16, quinn-proto 0.11.18, h2 0.4.16, and webpki-root-certs 1.0.10, plus
  aws-lc-rs 1.19.0 and aws-lc-sys 0.47.0 for rustls. PyO3 stays on 0.28.3 until Switchyard
  moves to 0.29; the extension doesn't call the APIs that GHSA-36hh-v3qg-5jq4 and
  GHSA-chgr-c6px-7xpp affect.
- The image installs pyasn1 0.6.5 and pypdf 6.21.4, which fix newly published high-severity
  advisories in the base image's pyasn1 0.6.4 and the previously patched pypdf 6.21.2.
- The gateway image removes the `openssl` command-line package, which nothing in the image
  uses, and raises the minimum version of `python-3.13` to 3.13.18 for CVE-2026-115557 and
  of `libexpat1` to 2.10.0 for CVE-2026-113351 and CVE-2026-115506. The fixed Python links
  OpenSSL 4.0, whose configuration files the removed package owned.
- The gateway plugin rejects a request whose body carries its own credential (`api_key`,
  `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`, or an `Authorization`
  entry in `extra_headers`, `headers` or `provider_specific_header`, the three header
  containers LiteLLM merges into the outbound request, as a mapping or a list of pairs)
  with HTTP 400 before routing. The check walks the whole body, so a credential inside
  `extra_body` or a per-request `fallbacks`, `context_window_fallbacks` or
  `content_policy_fallbacks` dict target, which the router merges into the call, is
  rejected too; `messages`, `tools`, `functions`, `tool_choice` and `response_format` are
  content and are not inspected.
  LiteLLM 1.102.2 otherwise keeps a body `api_key` as an extra deployment of the model
  group and reuses it for other callers' later requests, and sends a caller's
  `Authorization` header in place of the task role's signature.
- Profile validation fails if a profile sets `allow_client_side_credentials` (in
  `general_settings` or `litellm_settings`) or a deployment's
  `configurable_clientside_auth_params`, the two settings that turn off LiteLLM's
  request-body guard.

## 0.1.0 - 2026-10-15

### Added

- Repository scaffold: AWS CDK (TypeScript) project, aws-samples community files,
  MIT-0 license, CI configuration.
- `docker/switchyard-server/Dockerfile`: builds `switchyard-server` 0.3.0 from crates.io on
  a slim Rust image into a non-root `debian:bookworm-slim` runtime.
- `config/litellm.yaml` with ten Amazon Bedrock aliases (Claude Opus 5.5, Claude Sonnet 5.5,
  GPT-5.5, GPT-5.6 Luna, Kimi K3, GLM 5.3, Qwen3 Coder Next, DeepSeek V3.2, MiniMax M2.5,
  gpt-oss-120b) and `config/switchyard.toml` with the LiteLLM clients and targets.
- `compose.yaml` for a local run of the two-container task, plus `lib/routing-config.ts`,
  which loads both files and validates them against each other at synth and test time.
- Full Switchyard route catalog: `auto`, `task`, `task-gpt-oss`, `task-claude-gpt`
  (Task routing across the Claude+OSS, GPT+OSS, and Claude+GPT pairs), `execution`,
  `composite`, `random-oss`, and a passthrough baseline per model.
- `scripts/smoke-test.sh`: checks health, the model list, key enforcement, every route's
  selected model, Task routing on an easy and an under-specified prompt, and Execution
  routing escalation driven by `scripts/fixtures/stage-escalation.json`.
- AWS CDK stack (`SwitchyardGateway`): VPC with a Bedrock Runtime endpoint, a task role
  scoped to the configured models, one Fargate task running `switchyard-server` and
  LiteLLM, an allowlisted Application Load Balancer (HTTP, or HTTPS with ACM and Route 53
  when a domain is given), AWS WAF, CloudWatch log groups, and a Secrets Manager master key.
- cdk-nag AwsSolutions checks with documented acknowledgments, and a GitHub Actions
  workflow (lint, type-check, tests, synth, audit, shellcheck, image build, secret scan).
- `scripts/preflight.sh`: read-only checks of the toolchain, AWS identity and Region, CDK
  bootstrap, Docker build platform, and per-model Amazon Bedrock availability and
  authorization for every model in `config/litellm.yaml`.
- `bench/`: a standard-library coding agent with four Python tasks that measures cost per
  task and cost per solved task per route, priced from Switchyard's per-session statistics
  (which include the Task routing judge call).
- Documentation: README (architecture, route catalog, model table, deploy, test, agent
  setup, bench, configuration, security, cost, clean-up, limitations),
  `docs/TROUBLESHOOTING.md`, and `docs/DECISIONS.md`.
- `assets/architecture.drawio` and `assets/architecture.png`: the architecture diagram showing
  the Switchyard server and LiteLLM containers in one Fargate task.
- NVIDIA Nemotron 3 Super 120B and Nemotron 3 Nano 30B aliases in `config/litellm.yaml`
  (in-Region model IDs; no cross-Region inference profile exists in `us-east-1`) and seven
  routes in `config/switchyard.toml`: `auto-nemotron`, `task-nemotron`, `execution-nemotron`,
  `execution-nemotron-nano`, `composite-nemotron`, and a passthrough for each model. Each
  pairs Claude Opus 5.5 with a Nemotron model, for comparison with the Kimi K3 routes.
- `bench/multiturn.py`: runs the same agentic coding loops and scripted seven-turn chats
  through every route, and records each model turn (served model from the response header
  and body, tokens, latency, HTTP status, retries, test or check result) and each
  conversation's cost from Switchyard's session statistics. Spend caps per run and per
  conversation, and exponential backoff with jitter for throttled calls.
- `docs/TROUBLESHOOTING.md`: models that reject the `temperature` field.

### Changed

- `bench/run.py`: prices for the two Nemotron aliases, and the unit tests run in an
  isolated Python interpreter with a minimal environment, so model-written code cannot read
  the gateway key or AWS credentials from the environment.
- README: the route and model tables, the smoke test, and the bench section cover the
  Nemotron routes and `bench/multiturn.py`.
- `.gitignore`: ignore local `.agents/` working notes.

This release was deployed to a non-production account in `us-east-1` (7 min 49 s) and
checked from outside AWS: all 17 routes returned `200` with the expected selected model,
Task routing sent under-specified prompts to the capable tier on all three pairs, Execution
routing escalated on the repeated-failure fixture, no-key requests got `401`, and the
`slugify` bench solved on the capable-only, efficient-only, and `task` routes. The
deployment was destroyed afterwards. Model IDs were checked in `us-east-1`: Kimi K3 and GLM 5.3
are available as global cross-Region inference profiles, so every Claude, GPT, Kimi, and GLM
alias used a global profile; Qwen3 Coder Next, DeepSeek V3.2, MiniMax M2.5, and gpt-oss-120b
used in-Region IDs, and Qwen3 Coder Next is not offered in `us-west-2`.
