import * as path from 'node:path';
import {
  groupModels,
  loadLiteLLMConfig,
  loadSwitchyardConfig,
  profileDirectory,
  ROUTED_GROUP,
  validateRoutingConfig,
} from '../lib/routing-config';
const profile = process.argv[2] ?? 'stage';
try {
  const directory = profileDirectory(profile);
  const litellm = loadLiteLLMConfig(path.join(directory, 'litellm.yaml'));
  const switchyard = loadSwitchyardConfig(path.join(directory, 'switchyard.toml'));
  const problems = validateRoutingConfig(litellm, switchyard);
  if (problems.length) throw new Error(problems.join('\n'));
  const manifest = {
    profile,
    algorithm: switchyard.algorithm,
    modelGroup: ROUTED_GROUP,
    models: groupModels(litellm, ROUTED_GROUP),
    baselines: Object.fromEntries(
      litellm.models
        .filter((model) => model.modelName !== ROUTED_GROUP)
        .map((model) => [model.modelName, model.model]),
    ),
  };
  console.log(JSON.stringify(manifest));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
