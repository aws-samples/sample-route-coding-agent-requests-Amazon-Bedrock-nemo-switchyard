# Migrating to the LiteLLM plugin

The active deployment now runs one LiteLLM container with the Switchyard routing plugin. The standalone server, its route catalog, and its session-statistics endpoints are no longer part of the deployment.

| Previous sample                                           | Plugin sample                                                  |
| --------------------------------------------------------- | -------------------------------------------------------------- |
| `model: task`, `execution`, or another server route       | The routed model group in the active YAML, always `switchyard` |
| `config/litellm.yaml` and server `config/switchyard.toml` | `profiles/<name>/litellm.yaml` and plugin `switchyard.toml`    |
| `x-model-router-selected-model`                           | `x-litellm-model-name`                                         |
| Server on port 8080 forwards to LiteLLM                   | ALB targets LiteLLM on port 4000                               |
| Task and Composite judge calls                            | Stage or Random selected at deployment                         |
| Server session statistics                                 | Usage and selected model recorded from each response           |

Keep the full conversation, including tool calls and results, in each Stage request. A session ID does not replace that history. The response body's model remains the public group; use the response header to identify the selected candidate.

The image pins Switchyard source revision `21075c6652152dac88a5fbc6b878ca37d7617c1c` and LiteLLM 1.102.2. The source archive has a checksum in the Dockerfile. The plugin is experimental and currently depends on source-only APIs; do not substitute a published package without verifying compatibility.

Before updating an existing stack, select a profile, run validation and preflight, and inspect `cdk diff`. Deploy to the same account, Region, and stack name. The container and target-group configuration change, and the model permissions are regenerated from the selected profile.

## Keep the old Switchyard logs

Add `-c retainLegacyLogs=true` to the first migration diff and deploy commands to preserve the old `/<stack name>/switchyard` log group. Without the flag, the migration update deletes it. A later update can omit the flag; the group then stays in your account, retained outside the stack, and you delete it yourself when you no longer need it.

Use the flag only for that first migration of an existing stack:

- On a fresh deployment, the flag creates an empty `switchyard` log group that nothing writes to. It's retained, so it outlives `cdk destroy`.
- After an update without the flag has left the group outside the stack, deploying with the flag again fails with `AlreadyExists`, because CloudFormation tries to create a group with the same name. Leave the flag off, or delete the retained group first if you don't need its logs.

## Update clients and scripts

The default pair is Claude Opus 5.5 and Nemotron 3 Super 120B. Remove old route IDs from clients and update scripts that read the old header or statistics endpoints. Previous benchmark results describe the standalone-server implementation.

Each profile now routes the `switchyard` group and adds one baseline group per routed model (`claude-opus-5-5`, `nemotron-super-3-120b`) for single-model comparisons in the same deployment. The image wraps the Switchyard plugin so groups with one model pass through; register `switchyard_gateway.configured.ROUTING_PLUGIN` in custom profiles. Bench commands that named server routes need model group names instead, for example `python3 bench/run.py --routes switchyard claude-opus-5-5 nemotron-super-3-120b`; without `--routes`, the runner compares every group the gateway lists.
