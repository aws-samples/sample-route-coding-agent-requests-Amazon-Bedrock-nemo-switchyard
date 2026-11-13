# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.

## Development checks

The gateway is one LiteLLM container with the Switchyard routing plugin, built from `docker/gateway/`. Model and routing settings live in `profiles/<name>/`. When you change gateway settings, update both example profiles (`stage` and `random`).

Run these from the repository root:

```bash
npm ci
npm run lint
npm run build
npm test
npm run test:python
npm run validate:profile -- stage
npm run validate:profile -- random
docker build -t switchyard-litellm:local docker/gateway
docker run --rm --network none --entrypoint python \
  -e REPO_ROOT=/repo -e AWS_REGION=us-east-1 -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/repo:ro" switchyard-litellm:local \
  -m unittest discover -s /repo/docker/gateway/tests -v
npm run test:proxy
```

None of these need AWS credentials or make model calls. `npm test` synthesizes the stack in Jest. The `docker run` step runs the routing tests inside the image with networking off. `npm run test:proxy` starts the image against a local mock provider and checks authentication, Stage selection and escalation, Random candidate boundaries, the baseline groups, `/v1/models`, and a complete coding-task artifact flow. Run unit tests from `test/python`, not over `bench/tasks`; the task fixtures fail on purpose until a model fixes them.

The proxy test reads these optional variables:

| Variable            | Default                    | Use                                                                                                                                                         |
| ------------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GATEWAY_IMAGE`     | `switchyard-litellm:local` | Image to test.                                                                                                                                              |
| `E2E_READY_TIMEOUT` | `180`                      | Seconds to wait for LiteLLM to report healthy. A loaded host can take more than a minute.                                                                   |
| `E2E_PROVIDER_HOST` | detected                   | Host IP address the container can reach for the mock provider. Required for rootless Docker, which doesn't forward `host.docker.internal` to host loopback. |
| `E2E_TMPDIR`        | `.e2e-tmp/`                | Directory for the bind-mounted test config. It must be shared with the Docker VM; Colima and Docker Desktop share only parts of the host filesystem.        |

The mock provider binds 127.0.0.1 on macOS, Docker Desktop, and Colima, or the Docker bridge gateway on Linux Docker Engine. The proxy container publishes on 127.0.0.1 only.

Checks against AWS are separate. `scripts/preflight.sh` makes read-only AWS calls and no inference requests; `scripts/smoke-test.sh` sends billable Bedrock requests through a local or deployed gateway. See [Run it locally](README.md#run-it-locally) in the README, and the [validation record](docs/VALIDATION.md) for what has been exercised.

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

- A reproducible test case or series of steps
- The version of our code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the _main_ branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure the [development checks](#development-checks) pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional documentation on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security issue notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
