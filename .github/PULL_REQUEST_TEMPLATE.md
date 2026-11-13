_Issue #, if available:_

_Description of changes:_

_Checklist:_

- [ ] `npm run lint`, `npm run build`, `npm test`, and `npm run test:python` pass locally
- [ ] If a file under `profiles/<name>/` changed: `npm run validate:profile -- <name>` and `ROUTING_PROFILE=<name> scripts/preflight.sh` pass
- [ ] If `docker/gateway/` or a profile changed: the image builds (`docker build -t switchyard-litellm:local docker/gateway`) and `npm run test:proxy` passes
- [ ] If the CDK code changed: `npx cdk synth -c allowedCidr=203.0.113.0/24 -c allowPlainHttp=true` succeeds and the cdk-nag tests pass
- [ ] Documentation (README, docs/) updated for user-visible changes
- [ ] CHANGELOG.md updated
- [ ] No account IDs, keys, or other secrets in the change

By submitting this pull request, I confirm that you can use, modify, copy, and redistribute this contribution, under the terms of your choice.
