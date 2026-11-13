# Design decisions

The sample runs the Switchyard routing plugin inside one LiteLLM container on Amazon ECS with AWS Fargate. LiteLLM handles authentication and provider calls; Switchyard selects a candidate model. The [gateway Dockerfile](../docker/gateway/Dockerfile) pins LiteLLM and the Switchyard source revision, builds the Rust bindings, and includes the upstream license and notice.

## Models and routing live in deployment profiles

Each profile pairs `litellm.yaml` with `switchyard.toml`. YAML defines the public model group and candidate models; TOML defines the selection policy. The sample provides Stage and Random profiles using the same two models. Stage assigns the capable role to the first candidate and the efficient role to the second.

The stack validates both files together and derives Bedrock invocation permissions from the selected model inventory. This keeps model configuration and IAM permissions together when a model changes.

## Every model goes through the Converse API with the bundled cost map

**Context.** With a bare `bedrock/<id>` model string, LiteLLM picks Converse or InvokeModel per model from its cost map. On LiteLLM 1.102.2, as on 1.102.0, `bedrock/global.anthropic.claude-opus-5-5` goes to InvokeModel and depends on a cost-map entry, while `bedrock/converse/global.anthropic.claude-opus-5-5` goes to Converse. Unless told otherwise, LiteLLM also downloads the cost map at startup, so the same image could route differently from one day to the next.

**Decision.** Every model is `bedrock/converse/<id>`, and validation rejects anything else. The image and Compose set `LITELLM_LOCAL_MODEL_COST_MAP=True`, so LiteLLM uses the cost map bundled in the pinned image.

**Consequences.** Request shape, tool calls, and usage look the same for every candidate, so a model swap doesn't change how the gateway talks to Bedrock. Startup makes no cost-map download, and routing and model limits change only when the image changes. A model that Converse doesn't support can't be added without changing this rule.

## Single-model baselines run beside the routed group

**Context.** Comparing routing with each model alone is only fair when every arm uses the same model strings, Region, and gateway settings. LiteLLM runs routing plugins on every model group, though, and the Stage plugin raises an error for any group without exactly two unique candidates, so a one-model group can't sit beside the routed group with the upstream plugin alone.

**Decision.** The image adds a small wrapper, `switchyard_gateway`, registered as `switchyard_gateway.configured.ROUTING_PLUGIN`. It hands groups with two or more unique candidates to the configured Switchyard plugin and passes single-model groups through unchanged. Each profile defines one baseline group per routed model, named by its alias, and validation requires it. Only `switchyard` may list more than one deployment, and the upstream plugin may not be registered beside the wrapper, which would load a second, unwrapped instance.

**Consequences.** One deployment serves `switchyard`, `claude-opus-5-5`, and `nemotron-super-3-120b`, and the bench compares them by default. The wrapper is code this sample owns on top of an experimental upstream API; it has in-image tests and an import check in the Dockerfile, and it has to be revisited when the Switchyard pin moves.

## Configuration changes roll out through ECS

The task definition carries the selected configuration text in environment variables. The startup script writes it to temporary files before starting LiteLLM. Profile changes roll out with a new task definition; the image can be reused when only configuration changes. Credentials stay out of the profile files.

## Gateway authentication uses a generated key

Secrets Manager generates a random suffix. ECS injects it at startup, and the container adds the `sk-` prefix to construct the LiteLLM master key. Bedrock calls use the task IAM role.

All callers share the gateway key. The sample has no database or virtual-key setup. Changing the secret requires restarting the tasks so they receive the new value.

## The load balancer forwards only the API paths

**Context.** LiteLLM serves much more than the OpenAI-compatible API: an admin UI, Swagger, ReDoc, and OpenAPI docs, key and model management routes, and `/health`, which calls every model when given a key. Through LiteLLM directly, `/ui`, `/redoc`, `/openapi.json`, `/sso/key/generate`, and `/health/readiness` answer without a key. LiteLLM's own switches don't close all of them: with `DISABLE_ADMIN_UI` set, 1.102.2 still serves the `/ui` static files.

**Decision.** The load balancer forwards `/v1/chat/completions`, `/v1/models`, and `/health/liveliness` (`ALLOWED_PATHS` in `lib/gateway-service.ts`) and answers every other path with its own 404. The container also sets `NO_DOCS`, `NO_REDOC`, `NO_OPENAPI`, and `DISABLE_ADMIN_UI`. Compose publishes the local port on `127.0.0.1` only.

**Consequences.** Only the routes the sample needs are reachable, whatever LiteLLM adds in a later release. Clients that use the Responses or Messages APIs, or `models.retrieve`, get a 404 until someone adds the path, tests it against the local container, and redeploys. The smoke test checks that the closed paths answer the load balancer's 404.

## Tasks run privately behind restricted ingress

The load balancer accepts connections from the configured IPv4 CIDR; the stack rejects a world-open range. AWS WAF adds reputation, known-bad-input, and rate rules. The listener serves HTTPS with an ACM certificate, and plain HTTP needs an explicit opt-in for temporary evaluation.

The task runs in private subnets. A Bedrock Runtime interface endpoint provides private model access, while a NAT gateway provides outbound access for image pulls and other AWS services.

## HTTPS is the default; plain HTTP needs an explicit opt-in

**Context.** Every request carries the shared master key in its `Authorization` header, the prompts, and the tool results and source code a coding agent sends, and the responses come back the same way. Over plain HTTP, anyone on the network path can read or change all of it. Not every reader has a Route 53 public hosted zone, and the ways to get TLS without a domain add AWS services to the stack.

**Decision.** Synth fails unless the deployment chooses a listener:

- `domainName` with `hostedZone`: CDK creates a DNS-validated ACM certificate and the Route 53 alias record.
- `domainName` with `certificateArn`: the load balancer uses an existing ACM certificate in the stack's Region, for a domain whose DNS can be anywhere. The stack creates no certificate and no DNS record, and the `LoadBalancerDnsName` output names the target for the reader's CNAME record.
- `allowPlainHttp=true`, with no HTTPS setting: plain HTTP on port 80 for a short test, with a synth-time warning (`switchyard-gateway:plain-http-listener`) and a `GatewayUrl` output description that says so.

Both HTTPS modes use the same listener, TLS policy, and port 80 redirect. Ambiguous combinations fail: `certificateArn` without `domainName` or beside `hostedZone`, and `allowPlainHttp=true` beside any HTTPS setting. `cdk.json` never sets `allowPlainHttp`.

**Consequences.** A deployment is encrypted unless the deployer types the opt-in, and the opt-in path keeps the controls it had: the `allowedCidr` allowlist, which rejects world-open ranges, AWS WAF, the three-path allowlist, no AWS WAF sampled requests, the warnings in the README and SECURITY.md, and the tested key rotation steps. Existing stacks deployed over HTTP need `-c allowPlainHttp=true` on their next deploy, or the HTTPS settings to move to HTTPS.

## LiteLLM upgrades are manual

**Context.** LiteLLM handles every request the load balancer forwards. The Switchyard LiteLLM plugin targets the LiteLLM 1.102.x line (its upstream example pins 1.102.0; the image uses the 1.102.2 patch release), and the Dockerfile asserts the exact LiteLLM version at build time.

**Decision.** Dependabot ignores the LiteLLM base image, so a LiteLLM upgrade is a deliberate change: move the base image digest, the version assert, and the security patch list together (see [Bumping pinned images and packages](../README.md#bumping-pinned-images-and-packages)), rebuild, and run the in-image routing tests and `npm run test:proxy`. CI builds the image but doesn't scan it for vulnerabilities.

**Consequences.** Fixes in newer LiteLLM releases don't arrive by themselves. Check LiteLLM's [security advisories](https://github.com/BerriAI/litellm/security/advisories), and scan the image before you deploy. From the repository root, this builds the image and scans it with Trivy, pinned by version and digest:

```bash
docker build -t switchyard-litellm:local docker/gateway
docker save switchyard-litellm:local -o "$HOME/switchyard-litellm-local.tar"
docker run --rm -v "$HOME/switchyard-litellm-local.tar:/image.tar:ro" \
  public.ecr.aws/aquasecurity/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c \
  image --input /image.tar --severity HIGH,CRITICAL
```

Trivy downloads its vulnerability database when it runs. Delete the image tarball afterwards.

## The image patches the LiteLLM base image in place

**Context.** LiteLLM publishes its image only on the GitHub Container Registry (`ghcr.io`), and the LiteLLM 1.102.2 image ships Python and Wolfi packages with known vulnerabilities. A newer LiteLLM minor release would leave the 1.102.x line that the plugin targets.

**Decision.** The Dockerfile pins every base image by digest, and the Rust builder comes from the Docker Official Image mirror on Amazon ECR Public. On top of the LiteLLM image, the build:

- Removes the `openssl` command-line package, which nothing in the image uses. Its configuration files blocked the Python release that fixes CVE-2026-115557, because that release links OpenSSL 4.0. The OpenSSL 3.6 libraries stay for `apk-tools` and `libevent`; Python loads OpenSSL 4.0.
- Upgrades only the Wolfi packages that have fixed releases (`apk add --upgrade` with minimum versions), including Python 3.13.18 and libexpat 2.10.0. A full `apk upgrade` would replace the rest of the base image as well.
- Installs fixed releases of the vulnerable Python packages from `requirements-cve-patches.txt`, checked against hashes, without their dependencies.
- Removes two components the gateway doesn't use. LiteLLM imports the Datadog tracer (`ddtrace`) only when `USE_DDTRACE` is set, and its fixed release needs a package that PyPI doesn't offer. Only LiteLLM's Prisma database CLI uses Node.js.
- Fails if `uv pip check` reports any dependency conflict other than the base image's own `langfuse`/`packaging` one.

uv comes from PyPI, checked against hashes, and is mounted only for the install steps.

**Consequences.** LiteLLM's code stays as released; Python and its OpenSSL come from the Wolfi releases that fix the base image's advisories. [Known limitations](../SECURITY.md#known-limitations) covers the vulnerabilities that the patches leave. LiteLLM's database features and Datadog tracing aren't available. The patch list has to follow the base image: when a new base already ships a patched version, delete that entry, or the `--no-deps` install downgrades it. The LiteLLM base and the Dockerfile frontend still come from registries outside Amazon ECR Public, pinned by digest so that a moved tag can't change the build.

## Validation covers the configured sample

The coding demo records response usage and selected models without a server statistics endpoint. Saved workspaces and transcripts stay under the ignored results directory. Model-written Python runs locally without an operating-system sandbox.

See the [validation record](VALIDATION.md) for tested configurations and coverage limits, and [Plugin migration](PLUGIN-MIGRATION.md) for changes existing deployments need to make.
