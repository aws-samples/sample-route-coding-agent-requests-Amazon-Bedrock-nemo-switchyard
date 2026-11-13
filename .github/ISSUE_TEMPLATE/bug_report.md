---
name: Bug report
about: Something in the sample does not work as documented
title: ''
labels: bug
assignees: ''
---

**Describe the bug**
A clear and concise description of what the bug is.

**To reproduce**
Steps to reproduce the behavior, for example:

1. `npx cdk deploy -c allowedCidr=... -c domainName=... -c hostedZone=...` (or `-c certificateArn=...`, or `-c allowPlainHttp=true`)
2. `scripts/smoke-test.sh`
3. See error

**Expected behavior**
What you expected to happen.

**Logs and output**
Relevant output from `cdk deploy`, `scripts/preflight.sh`, the ECS task logs in
CloudWatch (`/SwitchyardGateway/litellm`), `docker compose logs litellm` for a
local run, or the response body and headers (including `x-litellm-model-name`). Remove account IDs, keys, and other secrets first.

**Environment**

- OS and shell:
- Node.js version (`node -v`):
- AWS CDK version (`npx cdk --version`):
- Docker version and host CPU architecture:
- AWS Region:
- `cpuArchitecture` context value (arm64 / x86_64):
- Routing profile (`profiles/<name>/`, for example `stage` or `random`):
- Models changed in the profile's `litellm.yaml`? (yes / no)
- Does `npm run validate:profile -- <name>` pass? (yes / no)

**Additional context**
Anything else that helps, such as changes you made to the profile's `switchyard.toml`,
or whether `npm run test:proxy` passes with your image.
