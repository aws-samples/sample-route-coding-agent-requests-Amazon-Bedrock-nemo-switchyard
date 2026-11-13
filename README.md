# Route coding agent requests to the right model on Amazon Bedrock with NVIDIA Switchyard

Deploy an OpenAI-compatible LiteLLM gateway in your AWS account, using NVIDIA Switchyard routing to select which Amazon Bedrock model handles each request. The sample runs on Amazon ECS with AWS Fargate. LiteLLM provides the gateway, authentication, and provider communication; the Switchyard plugin supplies the model-selection logic.

This repository is the sample code for the AWS blog post _Route coding agent requests to the right model on Amazon Bedrock with NVIDIA Switchyard_.

> **Sample code.** This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory and compliance requirements before deployment. Don't deploy it to production without additional security testing. It's a starting point for learning and evaluation, not a drop-in production system. Deploy it to a non-production account, review the [Security](#security) section and the [Before production](#before-production) list, and read the [Limitations](#limitations) before you depend on it. The Switchyard LiteLLM integration is experimental, and its configuration and APIs can change between releases.

## Table of contents

- [Why a router in front of Bedrock](#why-a-router-in-front-of-bedrock)
- [Architecture](#architecture)
- [Request flow](#request-flow)
- [Supported routing strategies](#supported-routing-strategies)
- [Models](#models)
- [Prerequisites](#prerequisites)
- [Repository layout](#repository-layout)
- [Run it locally](#run-it-locally)
- [Deploy the gateway](#deploy-the-gateway)
- [Get the key and send your first request](#get-the-key-and-send-your-first-request)
- [Run a coding task](#run-a-coding-task)
- [Change a model and redeploy](#change-a-model-and-redeploy)
- [Bumping pinned images and packages](#bumping-pinned-images-and-packages)
- [Security](#security)
- [Before production](#before-production)
- [Cost and cleanup](#cost-and-cleanup)
- [Limitations](#limitations)
- [License](#license)

## Why a router in front of Bedrock

You ask a coding agent for a change, and it reads files, edits code, and runs tests until the work is done. That might take a few model calls or dozens. The price per token won’t tell you what the whole task will cost. A capable model might cost more per token but finish in fewer turns. An efficient model might be cheaper per token but need more attempts, get stuck in a loop, or fail to finish.

NVIDIA Switchyard is an open-source library that helps an AI agent choose which model handles each request. You define the models it can use and the strategy it follows.

This sample integrates Switchyard into LiteLLM through a routing plugin. Your application connects to LiteLLM, which handles authentication, provider communication, and the Amazon Bedrock calls. Switchyard supplies the model-selection logic.

The documented [LiteLLM integration](https://github.com/NVIDIA-NeMo/Switchyard/blob/21075c6652152dac88a5fbc6b878ca37d7617c1c/examples/litellm/README.md) supports two strategies:

- **Stage routing:** Uses recent tool activity and results in the request history to select between capable and efficient models.
- **Random routing:** Selects a candidate model randomly, with optional weights, to support traffic splits and A/B comparisons.

## Architecture

![Gateway architecture: a coding agent sends OpenAI-compatible requests to an Application Load Balancer that allows only the configured IPv4 CIDR range and is protected by AWS WAF. The load balancer serves HTTPS with an ACM certificate by default and plain HTTP only by opt-in, and an optional Route 53 hosted zone holds the DNS record. One LiteLLM container on Amazon ECS with AWS Fargate, in private subnets, embeds the NeMo Switchyard plugin. Stage selects between a capable and an efficient candidate from request-history signals; Random selects from a configured candidate pool. LiteLLM calls the selected Amazon Bedrock model through the Converse API over a Bedrock Runtime VPC endpoint, using the task IAM role. The image comes from Amazon ECR, the master key from AWS Secrets Manager, and logs go to Amazon CloudWatch. A NAT gateway in the public subnets provides outbound access.](assets/architecture.png)

The gateway runs as a single LiteLLM container with the embedded NeMo Switchyard plugin on Amazon ECS with AWS Fargate. The diagram covers both example profiles: Stage chooses between a capable and an efficient candidate, and Random chooses from a configured candidate pool. In both shipped profiles the candidates are Claude Opus 5.5 and Nemotron 3 Super 120B.

The supporting resources are:

- **Ingress:** An Application Load Balancer forwards the API paths to LiteLLM and answers every other path with 404. Its security group only accepts connections from the configured network range, and an AWS WAF web ACL filters what gets through.
- **Networking:** An Amazon VPC spans public and private subnets across two Availability Zones. A NAT gateway gives the task outbound access, and a Bedrock Runtime interface VPC endpoint keeps calls to Amazon Bedrock off the public internet.
- **Container image:** Amazon ECR stores the LiteLLM image, with the Switchyard plugin and its dependencies baked in. The image removes the base image's Node.js runtime, so LiteLLM's database (Prisma) features aren't available in it; the sample doesn't use a database.
- **Permissions and credentials:** IAM task and execution roles scope what the container can do. AWS Secrets Manager holds the generated secret behind the gateway's master key.
- **Observability:** Amazon CloudWatch collects container logs and Container Insights metrics.
- **HTTPS:** AWS Certificate Manager provides the load balancer's TLS certificate. CDK either issues it for a hostname in an existing Route 53 public hosted zone and adds the alias record, or uses an existing certificate that you name by ARN. Plain HTTP needs an explicit opt-in.

## Request flow

The numbers match the diagram.

1. **The coding agent sends a request.** It sends an OpenAI-compatible request to the gateway with the `model` field set to `switchyard` and the shared gateway key in the `Authorization` header. The endpoint is your hostname over HTTPS, with an AWS Certificate Manager (ACM) certificate on the Application Load Balancer, restricted to the allowed IPv4 CIDR range. With a Route 53 hosted zone, Amazon Route 53 resolves the hostname to the load balancer. Plain HTTP on the load balancer's DNS name is an explicit opt-in for short tests.
2. **AWS WAF filters the request.** The web ACL on the load balancer applies IP reputation, known-bad-input, and per-IP rate-limit rules. The load balancer forwards allowed requests on the chat completions, model list, and liveness paths to an Amazon ECS task running on AWS Fargate in a private subnet, and answers every other path with 404.
3. **LiteLLM authenticates the request and Switchyard selects a model.** Each Fargate task runs LiteLLM and the embedded NeMo Switchyard plugin in a single container. LiteLLM validates the shared gateway key and invokes the plugin to select a model. The Stage profile uses request-history signals to choose between a capable and an efficient model, defaulting to the efficient model when signals are inconclusive, so the agent has to send the conversation history. The Random profile selects from a configured candidate pool instead. Neither profile calls a separate judge model.
4. **LiteLLM calls Amazon Bedrock.** LiteLLM translates the request and invokes the selected Bedrock model or inference profile through `Converse` or `ConverseStream`. The call uses the ECS task IAM role for credentials and passes through the Bedrock Runtime interface VPC endpoint, keeping inference traffic on the AWS network.
5. **The response returns to the agent.** The response or token stream returns through LiteLLM and the load balancer to the coding agent. The `x-litellm-model-name` response header identifies the selected model.

## Supported routing strategies

A deployment profile says which models are on the table and how Switchyard picks between them. The [LiteLLM integration](https://github.com/NVIDIA-NeMo/Switchyard/blob/21075c6652152dac88a5fbc6b878ca37d7617c1c/examples/litellm/README.md) includes Stage and Random to start.

| Strategy   | How it picks a model                                                                                                 | Plugin TOML            |
| ---------- | -------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| **Stage**  | Reads recent tool activity and results in the request history, then picks the configured capable or efficient model. | `algorithm = "stage"`  |
| **Random** | Picks at random from the configured models, with optional weights.                                                   | `algorithm = "random"` |

Your application calls the routed model group in `litellm.yaml`, which must be named `switchyard`, so you send `"model": "switchyard"` with every request. Which profile is active decides what happens next.

The walkthrough shows how to swap its models and settings, and how to set up a Random profile instead. Switchyard has more strategies than these two, though which ones you can use depends on the integration. The [routing overview](https://github.com/NVIDIA-NeMo/Switchyard/blob/21075c6652152dac88a5fbc6b878ca37d7617c1c/docs/routing_algorithms/overview.md) has the full catalog.

## Models

Both example profiles use the same two models. Stage assigns them the roles below. Random selects between them using the configured weights.

| Role in Stage | Model                        | Bedrock model or profile ID        |
| ------------- | ---------------------------- | ---------------------------------- |
| Capable       | Claude Opus 5.5              | `global.anthropic.claude-opus-5-5` |
| Efficient     | NVIDIA Nemotron 3 Super 120B | `nvidia.nemotron-super-3-120b`     |

Both profiles call each model through the Bedrock Converse API as `bedrock/converse/<id>`, and validation rejects any other route. This pair has been tested in `us-east-1`. Each model is also available on its own as a baseline group (`claude-opus-5-5`, `nemotron-super-3-120b`), so you can compare routing with either model alone; see [Compare cost per task](#compare-cost-per-task). To replace either model, edit the active profile’s `litellm.yaml` and follow [Change a model and redeploy](#change-a-model-and-redeploy).

For other options, see the [Bedrock model catalog](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html) and [LiteLLM Bedrock documentation](https://docs.litellm.ai/docs/providers/bedrock). Check model access, Regional availability, and tool-calling support for the coding examples before making a change. Validation covers the configured pair.

The `global.` prefix identifies a global cross-Region inference profile. Bedrock may process those requests in supported commercial AWS Regions worldwide. If processing location matters, choose a supported geographic profile or in-Region model and verify where requests can be processed. See [inference profile availability](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html).

## Prerequisites

- A non-production AWS account with access to the two selected Bedrock models.
- AWS CLI v2 with configured credentials and an explicit AWS Region. The example commands use `us-east-1`.
- Node.js 20 or later, npm, Docker with BuildKit, and Python 3.11 or later.
- A CDK-bootstrapped account and Region.
- A hostname you own, with either an existing public Route 53 hosted zone for its domain or an ACM certificate for it in the deployment Region. A short plain-HTTP test needs neither.

## Repository layout

The main files and directories are:

```text
.
├── profiles/           # Model and routing configuration
├── docker/gateway/     # Gateway container image and startup script
├── bin/app.ts          # CDK application entry point
├── lib/                # AWS infrastructure and configuration validation
├── scripts/            # Preflight checks, profile validation, and smoke tests
├── bench/
│   ├── run.py          # Coding-task runner and cost-per-task comparison
│   ├── multiturn.py    # Multi-turn coding and chat cost comparison
│   └── tasks/          # Task descriptions, starter code, and tests
├── test/               # Automated tests
├── docs/               # Troubleshooting, migration, and validation records
├── assets/             # Architecture diagram
└── compose.yaml        # Local gateway deployment
```

The [Change a model and redeploy](#change-a-model-and-redeploy) section walks through updating the model and routing configuration.

## Run it locally

You can try the gateway on your machine before deploying anything. Compose builds the same image and reads the same profile files that the stack deploys to ECS. Run from the repository root:

```bash
npm ci
cp .env.example .env

export LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"
eval "$(aws configure export-credentials --format env)"

ROUTING_PROFILE=stage docker compose up --build --wait
GATEWAY_URL=http://127.0.0.1:4000 ROUTING_PROFILE=stage scripts/smoke-test.sh
```

The key you export takes precedence over the placeholder in `.env`, so Compose and the smoke test use the same one. `.env` sets the Region (`AWS_REGION`), the profile (`ROUTING_PROFILE`), and the host port (`LITELLM_PORT`, default `4000`); a value exported in your shell wins over the file. If you change `LITELLM_PORT`, use that port in `GATEWAY_URL`. AWS credentials come only from your shell and never go in `.env` or the profile files. Swap `stage` for `random` to try Random routing.

Compose publishes the gateway on `127.0.0.1` only. There's no load balancer locally, so the smoke test skips its closed-path checks. The local gateway still calls Amazon Bedrock, and those calls are billed.

When you're finished:

```bash
docker compose down
```

## Deploy the gateway

Run from the repository root using your configured AWS credentials. Set `AWS_PROFILE` first if you use a named profile.

Your account and Region must be [bootstrapped for CDK](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-env.html). For an existing installation, follow [Plugin migration](docs/PLUGIN-MIGRATION.md) first.

The gateway serves HTTPS on a hostname you own. Set `GATEWAY_DOMAIN` to that hostname, and choose where its certificate comes from:

- **A Route 53 public hosted zone.** Set `HOSTED_ZONE` to an existing public hosted zone that contains the hostname. CDK creates a DNS-validated ACM certificate and the alias record.
- **An existing ACM certificate.** Set `CERTIFICATE_ARN` to an ACM certificate for the hostname in the deployment Region, for example when the domain's DNS isn't in Route 53. CDK creates no certificate and no DNS record. After the deployment, create a CNAME record (or a Route 53 alias) that points the hostname at the `LoadBalancerDnsName` output.

```bash
npm ci

export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION="$AWS_REGION"
eval "$(aws configure export-credentials --format env)"
export CDK_DEFAULT_REGION="$AWS_REGION"
export CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export ALLOWED_CIDR="$(curl -4 -fsS --connect-timeout 5 --max-time 15 https://checkip.amazonaws.com)/32"
export GATEWAY_DOMAIN=llm-gateway.example.com
export HOSTED_ZONE=example.com

scripts/preflight.sh
npx cdk diff -c allowedCidr="$ALLOWED_CIDR" \
  -c domainName="$GATEWAY_DOMAIN" -c hostedZone="$HOSTED_ZONE"
npx cdk deploy -c allowedCidr="$ALLOWED_CIDR" \
  -c domainName="$GATEWAY_DOMAIN" -c hostedZone="$HOSTED_ZONE"
```

With an existing certificate, replace `-c hostedZone="$HOSTED_ZONE"` with `-c certificateArn="$CERTIFICATE_ARN"` in both commands.

This deploys the default Stage configuration and allows access from your current public IPv4 address. Keep the allowlist to your own `/32`: everyone who shares an allowed address can reach the endpoint (see [Security](#security)). Preflight checks the configuration, tools, and AWS access without making inference requests. CDK builds the image, publishes it to ECR, and deploys the gateway. Deployment prints the gateway URL and commands to retrieve the generated API key and run the smoke test. The next section walks through your first request.

Always give clients the `https://` URL. Port 80 stays open only to redirect to HTTPS, and a client configured with `http://` has already sent the key and its first request in clear text when the redirect arrives.

### Quick test over plain HTTP

Without HTTPS settings, synth stops with an error. For a short test without a domain, opt in to plain HTTP on the load balancer's DNS name:

```bash
npx cdk deploy -c allowedCidr="$ALLOWED_CIDR" -c allowPlainHttp=true
```

> **Warning: plain HTTP.** With `allowPlainHttp=true`, the gateway listens on plain HTTP, and CDK prints a warning that says so whenever it synthesizes the stack. The master key (the `Authorization` header), your prompts, the tool results and source code your agent sends, and the model responses cross the network unencrypted between your client and the load balancer. Use HTTP only for a short test with non-sensitive code, and keep `allowedCidr` to your own `/32`. Then rotate the key (see [Security](#security)), and run `cdk destroy` when you finish (see [Cost and cleanup](#cost-and-cleanup)).

You can move the same stack to HTTPS later: deploy it again with the HTTPS settings and without `allowPlainHttp`.

**Upgrading an existing HTTP deployment:** a stack deployed without a domain before HTTPS became the default needs `-c allowPlainHttp=true` on its next `cdk diff` and `cdk deploy`, or the HTTPS settings to move it to HTTPS.

## Get the key and send your first request

The stack generates the gateway API key for you. In the same terminal used for deployment, load the gateway URL and key:

```bash
export STACK_NAME=SwitchyardGateway

output() {
  aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

export GATEWAY_URL="$(output GatewayUrl)"
export LITELLM_MASTER_KEY="sk-$(aws secretsmanager get-secret-value --region "$AWS_REGION" \
  --secret-id "$(output MasterKeySecretArn)" --query SecretString --output text)"
```

Send a request to the `switchyard` model group:

```bash
curl -sS -i --connect-timeout 10 --max-time 180 "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"switchyard","messages":[{"role":"user","content":"Reply with the word hello."}],"max_tokens":128}'
```

Look for HTTP 200 and an answer. The `x-litellm-model-name` response header shows which model handled the request. The response body’s `model` remains `switchyard`.

To check health, authentication, and routing, run:

```bash
ROUTING_PROFILE="$(output RoutingProfile)" scripts/smoke-test.sh
```

The request and smoke test make billable Bedrock calls.

## Run a coding task

The demo lets an agent read files, change source code, and run tests through the deployed model group:

```bash
python3 bench/run.py --list-tasks
python3 bench/run.py --tasks slugify --model switchyard --max-turns 6
```

Start with slugify. The terminal shows the task, selected models, tool actions, test results, and the final code diff. Each run saves its starting files, final workspace, transcript, patch, and result under `bench/results/`.

The other tasks cover Roman numerals, an arithmetic parser, and an LRU/TTL cache. See [Coding tasks](docs/CODING-TASKS.md) for the files, expected behavior, and saved artifacts.

To give the arithmetic parser task room to run tests and attempt repairs:

```bash
python3 bench/run.py --tasks calc --model switchyard --max-turns 12
```

One observed run using the default Stage model pair followed this sequence:

| Turns  | Selected model   | What happened                                                              |
| ------ | ---------------- | -------------------------------------------------------------------------- |
| 1 to 9 | Nemotron 3 Super | Built the parser and attempted repairs. Tests failed on turns 5, 7, and 9. |
| 10     | Claude Opus 5.5  | Read `calc.py`.                                                            |
| 11     | Nemotron 3 Super | Edited the parser to fix operator precedence.                              |
| 12     | Nemotron 3 Super | Ran the tests. All seven passed.                                           |

Opus’s visible action was a file read; Super made the final repair. This shows model selection changing during a task. Your run may select different models, take a different number of turns, or fail. The turn limit is a ceiling, not a target.

A coding task can pass or fail without changing tiers. The controlled escalation fixture and the coding task demonstrate different things.

### Compare cost per task

Each profile deploys single-model baseline groups (`claude-opus-5-5`, `nemotron-super-3-120b`) next to the routed `switchyard` group, so one deployment can compare routing with each model alone. Without `--model` or `--routes`, the runner calls every model group the gateway lists at `/v1/models`, `switchyard` first:

```bash
python3 bench/run.py --tasks slugify roman --repeats 3
```

After the runs, the runner prints one row per model group:

| Column        | Meaning                                                             |
| ------------- | ------------------------------------------------------------------- |
| `RUNS`        | Task runs on the group (tasks × repeats).                           |
| `SOLVED`      | Runs whose supplied tests passed at the end.                        |
| `ERRORS`      | Runs that ended with a request, response, or harness error.         |
| `COST/TASK`   | Estimated USD per run, from per-turn usage and the answering model. |
| `COST/SOLVED` | Estimated USD per solved run; `-` when no run was solved.           |

Each turn is priced from its `x-litellm-model-name` header against `PRICES` in `bench/run.py`. A group shows `n/a` with the reason when any of its runs is unpriced: a model without a `PRICES` entry, a turn with cached input (left unpriced instead of priced at uncached rates), a response without the selected-model header or token counts, or an unexpected harness error. A run that failed before its first turn counts as $0, with a note. A malformed tool call ends its run with an error; the comparison continues with the next run. Pass `--routes switchyard claude-opus-5-5` to compare a subset, or `--model <group>` for one group. Next to the per-run directories, the runner appends one summary record per run to `bench/results/run-<timestamp>.jsonl`. A few runs on two tasks show the workflow; they don't establish a cost or quality advantage.

### Compare multi-turn conversations

`bench/multiturn.py` runs the same conversations through every model group: an agentic loop over each coding task, plus four scripted chats of seven turns that alternate simple turns with design, debugging, and code-writing turns. Chat turns with a cheap check (expected text, a word limit, bullets, or assertions against the returned function) record whether the check passed. Results go to `bench/results/multiturn-<timestamp>.jsonl`, followed by a summary per model group:

```bash
python3 bench/multiturn.py --tasks slugify --scripts backend api --max-spend 2
```

`--max-conv-cost` (default 1 USD, `0` for no cap) caps each conversation and `--max-spend` stops starting new conversations. When either is set, a turn that can't be priced stops its conversation and the run, because its spend can't be counted. Harness errors are recorded and the run continues; a 200 response without a reply message counts as a turn error.

## Change a model and redeploy

Each directory under `profiles/` pairs two files: `litellm.yaml` defines the models and gateway settings; `switchyard.toml` controls how Switchyard selects a model.

### What’s in `litellm.yaml`

| Setting                                                    | What it does                                                           |
| ---------------------------------------------------------- | ---------------------------------------------------------------------- |
| `model_list`                                               | Lists the candidate deployments available to the gateway.              |
| `model_name`                                               | Names the public group your client requests, such as `switchyard`.     |
| `litellm_params.model`                                     | Identifies the backing model as `bedrock/converse/<id>`. Edit to swap. |
| `aws_region_name`                                          | Reads the AWS Region from the deployment environment.                  |
| `router_settings.plugins` and `litellm_settings.callbacks` | Register Switchyard for model selection and supported request changes. |
| `general_settings.master_key`                              | Reads the gateway API key from the environment.                        |

Both candidates share `model_name: switchyard`, so your client sends `"model": "switchyard"` and Switchyard selects a candidate. Stage requires two unique candidates: the capable model first and the efficient model second.

Each profile also has one baseline group per routed model, named by its alias (`claude-opus-5-5`, `nemotron-super-3-120b`). A baseline group has exactly one deployment and always answers with that model, so you can compare the routed group's cost and pass rate with each model alone in the same deployment, for example `"model": "nemotron-super-3-120b"`. The image registers `switchyard_gateway.configured.ROUTING_PLUGIN`, a small wrapper in `docker/gateway/switchyard_gateway/` that applies Switchyard to groups with two or more unique candidates and passes single-model groups through; the Stage plugin alone rejects them.

### What’s in `switchyard.toml`

| Setting                | What it does                                                             |
| ---------------------- | ------------------------------------------------------------------------ |
| `algorithm`            | Selects Stage or Random.                                                 |
| `picker`               | Sets Stage’s preferred tier when execution signals are inconclusive.     |
| `confidence_threshold` | Sets the confidence cutoff for Stage’s scored execution signals.         |
| `recent_window`        | Sets how many recent tool results Stage examines.                        |
| `weights`              | Sets Random’s optional selection weights in candidate declaration order. |

The Stage example uses `efficient_first`, a confidence threshold of `0.5`, and a window of three tool results.

### Swap a model

1. Copy the profile you want to customize. This example starts with Stage:

   ```bash
   cp -R profiles/stage profiles/my-profile
   ```

2. In `profiles/my-profile/litellm.yaml`, update `litellm_params.model` for each candidate you want to replace. Use `bedrock/converse/<id>` for every model, where `<id>` is the Bedrock model or inference profile ID, for example `bedrock/converse/global.anthropic.claude-opus-5-5`. The `converse/` route sends every request through the [Bedrock Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html), so tool calls and usage look the same for every candidate. Without it, LiteLLM picks the API per model from its cost map; see the [LiteLLM Bedrock documentation](https://docs.litellm.ai/docs/providers/bedrock).

   Check account access, Regional availability, and tool-calling support. Keep `model_name` unchanged so your client continues using the same public group. For Stage, preserve the capable-first, efficient-second order. Update the matching baseline group too: validation requires a one-model baseline group for every routed model.

   Never put credentials in a profile, such as `aws_access_key_id`, `aws_secret_access_key`, or `api_key` under `litellm_params`. The stack copies the whole `litellm.yaml` into the task definition and the CloudFormation template in plain text, and the task role already supplies the AWS credentials.

3. Add a `PRICES` entry in `bench/run.py` for each new model, with its Bedrock model ID and USD prices per million input and output tokens for your Region, so the cost comparison can price its turns. Turns from a model without an entry are reported as unpriced.

4. Keep `switchyard.toml` unchanged when replacing a model. Adjust it if you also want to change routing behavior. For Random, any configured weights must match the updated candidate list.

5. Validate the configuration and check model access:

   ```bash
   export ROUTING_PROFILE=my-profile
   scripts/preflight.sh
   ```

6. Rerun your original `cdk diff` and `cdk deploy` commands, adding `-c routingProfile="$ROUTING_PROFILE"` to both. Keep the same allowed CIDR, HTTPS (or `allowPlainHttp`), and CPU architecture settings. The stack updates Bedrock invocation permissions to match the selected models.

7. Check the updated deployment:

   ```bash
   scripts/smoke-test.sh
   ```

   The Stage smoke test assumes the supplied routing settings. If you change the routing policy, update its expected selections too.

To customize Random instead, start with `profiles/random/`. See the [pinned upstream example](https://github.com/NVIDIA-NeMo/Switchyard/blob/21075c6652152dac88a5fbc6b878ca37d7617c1c/examples/litellm/README.md) for additional routing settings.

## Bumping pinned images and packages

The gateway image changes only when a pin in `docker/gateway/` changes:

| Input                                      | Pin                                                                                               |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| Dockerfile frontend                        | `docker/dockerfile:1` by digest, from Docker Hub                                                  |
| Builder stage                              | `rust:1.99.1-slim-bookworm` by digest, from the Docker Official Image mirror on Amazon ECR Public |
| Runtime base                               | `ghcr.io/berriai/litellm:v1.102.2` by digest. LiteLLM publishes its image only on `ghcr.io`.      |
| Switchyard source                          | Commit `21075c6`, checked by sha256                                                               |
| Fixed Rust crates for the Switchyard build | `cargo update --precise` lines in the builder stage                                               |
| uv, used only while the image builds       | `requirements-uv.txt`, from PyPI with hashes                                                      |
| Fixed Python packages for the base image   | `requirements-cve-patches.txt`, from PyPI with hashes                                             |
| Fixed Wolfi packages for the base image    | Minimum versions on the `apk add` line                                                            |

The build also removes the Datadog tracer (`ddtrace`) and Node.js, which the gateway doesn't use, and fails when `uv pip check` reports a new dependency conflict. [Design decisions](docs/DECISIONS.md#the-image-patches-the-litellm-base-image-in-place) explains why.

Dependabot doesn't propose LiteLLM upgrades. To move to another release on the 1.102.x line that the plugin targets:

1. Read the new tag's index digest from the `Digest:` line of `docker buildx imagetools inspect ghcr.io/berriai/litellm:v1.102.N`.
2. In `docker/gateway/Dockerfile`, update the `FROM` line, the `version('litellm') == '1.102.N'` assert, and the version in the comments.
3. In `docker/gateway/requirements-cve-patches.in`, delete each package that the new base already ships at the pinned version or later, and compile the `.txt` file again. The patch install uses `--no-deps` and exact pins, so a stale entry would downgrade a newer package.

To change the Python patches or uv, edit the `.in` file, and compile it with [uv](https://docs.astral.sh/uv/) from `docker/gateway/`. Python 3.13 matches the base image's environment, and Python 3.11 matches the Debian builder:

```bash
uv pip compile requirements-cve-patches.in --no-deps --generate-hashes --universal \
  --python-version 3.13 -o requirements-cve-patches.txt
uv pip compile requirements-uv.in --no-deps --generate-hashes --universal \
  --python-version 3.11 -o requirements-uv.txt
```

Dependabot proposes updates to both requirements files every week. If the image build fails at the `uv pip check` step on one of those pull requests, the new version needs a newer dependency than the base image has, as aiohttp 3.14.4 needed yarl 1.25.1. Add that package to `requirements-cve-patches.in` at the minimum version the check names, and compile again.

For rust or the Dockerfile frontend, read the digest with `docker buildx imagetools inspect public.ecr.aws/docker/library/rust:<tag>` or `docker buildx imagetools inspect docker/dockerfile:1`, and update the `@sha256:` value. A new rust image invalidates the cached builder layers, so the next build compiles Switchyard again. For Wolfi packages, drop a minimum from the `apk add` line once the base image ships that version, and drop the `apk del openssl` step once the base image's Python already links OpenSSL 4.0. Don't replace the line with `apk upgrade`, which replaces the rest of the base image as well. When you move the Switchyard commit, delete each `cargo update` line whose crate the new `Cargo.lock` already has at that version or later, because `--precise` would otherwise downgrade it.

After any bump, run the [development checks](CONTRIBUTING.md#development-checks), including the in-image tests and `npm run test:proxy`, and scan the image as shown in [Design decisions](docs/DECISIONS.md#litellm-upgrades-are-manual).

## Security

This is evaluation code. Restrict the allowed CIDR, use HTTPS for ongoing access, and keep the master key and AWS credentials out of source control. LiteLLM enforces gateway authentication; the task role grants Bedrock invocation permissions derived from the selected profile. Cross-Region inference can process requests outside the deployment Region.

The master key is the gateway's only credential, and every caller shares it. Anyone who holds it is a gateway administrator who can call every model group with no spend cap. The AWS WAF rate rule allows 2,000 requests per 5 minutes per source IP, and it counts requests, not tokens. Before you deploy, create an AWS Budgets alert for Amazon Bedrock (see [Cost and cleanup](#cost-and-cleanup)), and run `cdk destroy` when you finish testing. Per-user keys with budgets are on the [Before production](#before-production) list.

LiteLLM 1.102.2 rejects request-body fields that redirect a call or switch its AWS role, such as `api_base`, `aws_bedrock_runtime_endpoint`, `aws_sts_endpoint`, and `aws_role_name`, with HTTP 401. The gateway's plugin rejects a request that carries its own credential, `api_key`, `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`, or an `Authorization` entry in any of the header containers LiteLLM merges into the call (`extra_headers`, `headers`, or `provider_specific_header`, as a mapping or a list of pairs), at any depth of the body (`extra_body` and per-request `fallbacks` targets included), with HTTP 400 before routing, because LiteLLM would otherwise keep a body `api_key` and use it for other callers' later requests to the same model group, and would send a caller's `Authorization` header in place of the task role's signature. Profile validation also fails if a profile turns LiteLLM's own request-body guard off (`allow_client_side_credentials` or `configurable_clientside_auth_params`). LiteLLM still accepts `aws_region_name` and `model_id`, so every call runs under the task role, which can invoke only the models that the **IAM** grant in [Security](SECURITY.md) covers.

The CIDR allowlist works at the network level only. Everyone behind an allowed address, such as an office or café NAT, a VPN exit, or a mobile carrier address, can reach the endpoint. Use your own `/32`, and treat the key as the access control.

The load balancer forwards three paths to LiteLLM: `/v1/chat/completions`, `/v1/models`, and `/health/liveliness`. Every other path gets the load balancer's own 404, so LiteLLM's admin UI, API docs, key and model management routes, and `/health` (which calls every model when given a key) aren't reachable through it. The smoke test checks that. Inside the container, LiteLLM's Swagger, ReDoc, and OpenAPI routes are off and `DISABLE_ADMIN_UI` is set. LiteLLM 1.102.2 still serves the `/ui` static files, so the allowlist is what blocks `/ui` in AWS. To forward another path, see [Troubleshooting](docs/TROUBLESHOOTING.md).

With `-c allowPlainHttp=true`, the gateway listens on plain HTTP, and CDK prints a synth warning that says so. The following cross the network unencrypted between your client and the load balancer: the master key in the `Authorization` header, your prompts, the tool results and source code your agent sends, and the model responses, including generated code and tool calls. Anyone on the network path can read them and can change responses, for example to add a tool call that your agent then runs. The CIDR allowlist doesn't prevent that. Use HTTP only for a short test with non-sensitive code from your own `/32`. Then rotate the key, and run `cdk destroy` when you finish. The tasks read the secret when they start, so rotation stores a new suffix, replaces the tasks, and loads the new key into your shell:

```bash
export AWS_REGION="${AWS_REGION:-us-east-1}"
export STACK_NAME="${STACK_NAME:-SwitchyardGateway}"

output() {
  aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

aws secretsmanager put-secret-value --region "$AWS_REGION" \
  --secret-id "$(output MasterKeySecretArn)" --secret-string "$(openssl rand -hex 24)"

aws ecs update-service --region "$AWS_REGION" --cluster "$(output ClusterName)" \
  --service "$(output ServiceName)" --force-new-deployment --query service.serviceName
aws ecs wait services-stable --region "$AWS_REGION" \
  --cluster "$(output ClusterName)" --services "$(output ServiceName)"

export LITELLM_MASTER_KEY="sk-$(aws secretsmanager get-secret-value --region "$AWS_REGION" \
  --secret-id "$(output MasterKeySecretArn)" --query SecretString --output text)"
```

The old key keeps working until ECS has replaced every task, which is when `services-stable` returns. Use the Region and stack name you deployed with.

For ongoing access, deploy with HTTPS (see [Deploy the gateway](#deploy-the-gateway)).

The coding demo executes model-written Python locally. Its credential-stripped subprocess environment is not an operating-system sandbox: the code can read any file your user can read and can reach the network. Use a disposable development environment where no long-lived AWS credentials are on disk. Protect the files under `bench/results/`, which contain prompts, model responses, tool output, and generated code.

See [Security](SECURITY.md) for reporting guidance and known limitations, and [Troubleshooting](docs/TROUBLESHOOTING.md) for common setup problems.

## Before production

The sample keeps these at its defaults to stay small. Work through them with your security team before any production use:

- **HTTPS only.** Deploy without `allowPlainHttp` ([Deploy the gateway](#deploy-the-gateway)), so no key, prompt, or code crosses the network in clear text.
- **Per-user keys with budgets.** Replace the shared master key with per-user LiteLLM virtual keys that carry budgets. They need a LiteLLM database, which this sample doesn't deploy.
- **Customer managed KMS keys.** The master key secret and the log group use AWS managed keys.
- **ALB access logs and VPC flow logs.** Both are off; the acknowledgments in `lib/nag-suppressions.ts` name the CDK calls that turn them on.
- **Container hardening.** Run the task with a read-only root file system (and a writable volume for `/tmp`, where `start.sh` writes the configuration), drop all Linux capabilities, and allow task egress on TCP 443 only.
- **A request-size limit.** AWS WAF inspects only the first 8 KB of a request body, and the sample has no body-size rule, so anyone inside the allowlist can send large request bodies to the task without a key.

## Cost and cleanup

AWS infrastructure and Bedrock inference incur charges. Check [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) for the selected models, Region, and inference options. The demo’s token costs are estimates from recorded successful responses. Cached input, unknown models, or missing usage are left unpriced; retries can add charges.

The stack sets no spend cap. Every key holder can call every model group, and the AWS WAF rate rule (2,000 requests per 5 minutes per source IP) counts requests, not tokens. A leaked key can therefore run up Bedrock charges, bounded only by your account's Bedrock quotas, until you rotate it. Before you deploy, [create an AWS Budgets alert](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) for Amazon Bedrock spend, and run `cdk destroy` as soon as you finish testing. For shared use, per-user keys with budgets are on the [Before production](#before-production) list.

When finished, delete the gateway stack using the same AWS account and Region as the deployment:

```bash
npx cdk destroy SwitchyardGateway \
  -c allowedCidr=203.0.113.0/24 -c allowPlainHttp=true \
  -c routingProfile="${ROUTING_PROFILE:-stage}"
```

CDK loads the app before deleting the stack, so it still requires a valid CIDR and a listener setting. The example range and `allowPlainHttp=true` above only satisfy that local validation during deletion, for HTTP and HTTPS stacks alike; they don't update the deployed gateway. They don't need to match the deployment.

Review retained resources and CDK bootstrap assets separately.

## Limitations

The plugin is experimental and pins source-only Switchyard APIs. The documented profiles support Stage and Random. Classifier-backed Task and Composite routing require intermediate model calls that this integration does not support.

Stage requires full supported request history and two unique candidates. The upstream adapter currently supports text messages, function tool calls, and text tool results; unsupported message shapes fail before inference. For single-model benchmark baselines, call the baseline groups (`claude-opus-5-5`, `nemotron-super-3-120b`) described in [What’s in `litellm.yaml`](#whats-in-litellmyaml).

The example does not promise production readiness or savings on every task. Only configurations and environments recorded in the [validation record](docs/VALIDATION.md) should be treated as tested.

## License

This sample is licensed under MIT-0. NVIDIA Switchyard and its plugin retain their upstream Apache-2.0 licensing. The image includes the Switchyard license and notice. [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) lists the third-party software the build pulls in. The LiteLLM image also contains an `enterprise/` directory whose license is not an open source license.
