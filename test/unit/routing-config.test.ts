import * as fs from 'node:fs';
import * as path from 'node:path';
import {
  groupModels,
  loadLiteLLMConfig,
  loadSwitchyardConfig,
  PLUGIN,
  PROFILES_DIR,
  profileDirectory,
  ROUTED_GROUP,
  validateRoutingConfig,
  type LiteLLMConfig,
  type SwitchyardConfig,
} from '../../lib/routing-config';

const PROFILE_NAMES = fs
  .readdirSync(PROFILES_DIR, { withFileTypes: true })
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name);

function profile(name = 'stage') {
  const directory = profileDirectory(name);
  return {
    litellm: loadLiteLLMConfig(path.join(directory, 'litellm.yaml')),
    switchyard: loadSwitchyardConfig(path.join(directory, 'switchyard.toml')),
  };
}
describe('plugin profiles', () => {
  test.each(['stage', 'random'])(
    '%s routes the same two Bedrock candidates and has a baseline for each',
    (name) => {
      const { litellm, switchyard } = profile(name);
      expect(validateRoutingConfig(litellm, switchyard)).toEqual([]);
      expect(groupModels(litellm, ROUTED_GROUP)).toEqual([
        'bedrock/converse/global.anthropic.claude-opus-5-5',
        'bedrock/converse/nvidia.nemotron-super-3-120b',
      ]);
      const baselines = litellm.models.filter((model) => model.modelName !== ROUTED_GROUP);
      expect(baselines.map((model) => [model.modelName, model.model])).toEqual([
        ['claude-opus-5-5', 'bedrock/converse/global.anthropic.claude-opus-5-5'],
        ['nemotron-super-3-120b', 'bedrock/converse/nvidia.nemotron-super-3-120b'],
      ]);
    },
  );
  test.each(PROFILE_NAMES)('%s registers the gateway wrapper in both plugin roles', (name) => {
    const { litellm } = profile(name);
    expect(PLUGIN).toBe('switchyard_gateway.configured.ROUTING_PLUGIN');
    expect(litellm.routerSettings.plugins).toEqual([PLUGIN]);
    expect(litellm.litellmSettings.callbacks).toContain(PLUGIN);
  });
  test.each(PROFILE_NAMES)('%s sends every model through the Bedrock Converse API', (name) => {
    const { litellm } = profile(name);
    expect(litellm.models.length).toBeGreaterThan(0);
    for (const model of litellm.models) expect(model.model).toMatch(/^bedrock\/converse\//);
  });
  test('rejects aliases that would split the routed candidate pool', () => {
    const { litellm, switchyard } = profile();
    const models = litellm.models.map((model, index) => ({ ...model, modelName: 'alias' + index }));
    expect(validateRoutingConfig({ ...litellm, models }, switchyard)).toEqual(
      expect.arrayContaining([expect.stringContaining('no "switchyard" model group')]),
    );
  });
  test('both plugin roles and gateway authentication are required', () => {
    const { litellm, switchyard } = profile();
    const problems = validateRoutingConfig(
      { ...litellm, routerSettings: {}, litellmSettings: {}, generalSettings: {} },
      switchyard,
    );
    expect(problems.join('\n')).toMatch(/plugins must be exactly/);
    expect(problems.join('\n')).toMatch(/callbacks must register/);
    expect(problems.join('\n')).toMatch(/master_key/);
  });
  test.each([[], [0, 0], [1], [-1, 2], [Infinity, 1]].map((weights) => [weights]))(
    'rejects invalid Random weights %j',
    (weights) => {
      const { litellm, switchyard } = profile('random');
      expect(
        validateRoutingConfig(litellm, {
          ...switchyard,
          settings: { algorithm: 'random', weights },
        }).join('\n'),
      ).toContain('Random weights');
    },
  );
  test('rejects standalone-server settings and unsupported algorithms', () => {
    const { litellm, switchyard } = profile();
    expect(
      validateRoutingConfig(litellm, {
        ...switchyard,
        settings: { ...switchyard.settings, routes: {} },
      }).join('\n'),
    ).toContain('Unknown stage setting');
    expect(
      validateRoutingConfig(litellm, { ...switchyard, algorithm: 'llm_classifier' }),
    ).toContain('algorithm must be stage or random');
  });
  test.each(['../stage', '/tmp', '', 'stage/other'])(
    'rejects profile path traversal %s',
    (name) => {
      expect(() => profileDirectory(name)).toThrow(/directory name/);
    },
  );
  test('rejects non-Bedrock models and embedded key configuration', () => {
    const { litellm, switchyard } = profile();
    const models = litellm.models.map((model) => ({
      ...model,
      model: 'openrouter/example',
      awsRegionName: 'hardcoded',
    }));
    const problems = validateRoutingConfig({ ...litellm, models }, switchyard).join('\n');
    expect(problems).toMatch(/Unsupported Bedrock/);
    expect(problems).toMatch(/aws_region_name/);
  });
});

describe('validateRoutingConfig with baselines', () => {
  const model = (modelName: string, m: string) => ({
    modelName,
    model: m,
    awsRegionName: 'os.environ/AWS_REGION',
  });
  const CAPABLE = 'bedrock/converse/global.anthropic.claude-opus-5-5';
  const EFFICIENT = 'bedrock/converse/nvidia.nemotron-super-3-120b';
  const THIRD = 'bedrock/converse/nvidia.nemotron-nano-3-30b';
  const litellm = (models: LiteLLMConfig['models']): LiteLLMConfig => ({
    models,
    routerSettings: { plugins: [PLUGIN] },
    litellmSettings: { drop_params: true, callbacks: [PLUGIN] },
    generalSettings: { master_key: 'os.environ/LITELLM_MASTER_KEY' },
    text: '',
  });
  const stage: SwitchyardConfig = {
    algorithm: 'stage',
    settings: { algorithm: 'stage', picker: 'efficient_first', confidence_threshold: 0.5 },
    text: '',
  };
  const random = (weights?: number[]): SwitchyardConfig => ({
    algorithm: 'random',
    settings: weights ? { algorithm: 'random', weights } : { algorithm: 'random' },
    text: '',
  });
  const pair = [
    model('switchyard', CAPABLE),
    model('switchyard', EFFICIENT),
    model('capable', CAPABLE),
    model('efficient', EFFICIENT),
  ];
  const trio = [...pair, model('switchyard', THIRD), model('third', THIRD)];

  test('accepts a consistent Stage profile', () => {
    expect(validateRoutingConfig(litellm(pair), stage)).toEqual([]);
  });

  test('Stage needs exactly two unique routed models', () => {
    expect(validateRoutingConfig(litellm(trio), stage)).toEqual([
      expect.stringContaining('exactly two'),
    ]);
  });

  test('Stage with one routed model is rejected', () => {
    const one = [model('switchyard', CAPABLE), model('capable', CAPABLE)];
    expect(validateRoutingConfig(litellm(one), stage)).toEqual([
      expect.stringContaining('exactly two'),
    ]);
  });

  test('Stage counts unique routed models, so a repeated deployment is accepted', () => {
    expect(validateRoutingConfig(litellm([...pair, model('switchyard', CAPABLE)]), stage)).toEqual(
      [],
    );
  });

  test('Random accepts two or more routed models with matching weights', () => {
    expect(validateRoutingConfig(litellm(trio), random([1, 1, 2]))).toEqual([]);
    expect(validateRoutingConfig(litellm(trio), random())).toEqual([]);
  });

  test('Random needs at least two routed models', () => {
    const one = [model('switchyard', CAPABLE), model('capable', CAPABLE)];
    expect(validateRoutingConfig(litellm(one), random())).toEqual([
      expect.stringContaining('at least two'),
    ]);
  });

  test('Random weights must match the unique routed models, not every deployment', () => {
    expect(validateRoutingConfig(litellm(pair), random([1, 2, 3])).join('\n')).toContain(
      'Random weights',
    );
    expect(validateRoutingConfig(litellm(pair), random([1, 2]))).toEqual([]);
  });

  test('requires the routed group', () => {
    expect(validateRoutingConfig(litellm(pair.slice(2)), stage)).toEqual(
      expect.arrayContaining([expect.stringContaining('no "switchyard" model group')]),
    );
  });

  test('requires a baseline for each routed model', () => {
    expect(validateRoutingConfig(litellm(pair.slice(0, 3)), stage)).toEqual([
      expect.stringContaining(EFFICIENT + ' has no baseline'),
    ]);
  });

  test('only the routed group may have more than one deployment', () => {
    const extra = [...pair, model('capable', EFFICIENT)];
    expect(validateRoutingConfig(litellm(extra), stage)).toEqual([
      expect.stringContaining('capable has 2 deployments'),
    ]);
  });

  test('rejects the upstream plugin path, which fails on single-model groups', () => {
    const upstream = 'switchyard_litellm.configuration.configured_plugin.ROUTING_PLUGIN';
    const cfg: LiteLLMConfig = {
      ...litellm(pair),
      routerSettings: { plugins: [upstream] },
      litellmSettings: { drop_params: true, callbacks: [upstream] },
    };
    const problems = validateRoutingConfig(cfg, stage).join('\n');
    expect(problems).toMatch(/router_settings\.plugins must be exactly \[switchyard_gateway/);
    expect(problems).toMatch(/litellm_settings\.callbacks must register switchyard_gateway/);
  });

  test('router_settings.plugins must be exactly the wrapper', () => {
    const cfg: LiteLLMConfig = {
      ...litellm(pair),
      routerSettings: { plugins: [PLUGIN, 'other.module.PLUGIN'] },
    };
    expect(validateRoutingConfig(cfg, stage)).toEqual([
      'router_settings.plugins must be exactly [' + PLUGIN + ']',
    ]);
  });

  test('callbacks must not also register the upstream plugin', () => {
    const upstream = 'switchyard_litellm.configuration.configured_plugin.ROUTING_PLUGIN';
    const cfg: LiteLLMConfig = {
      ...litellm(pair),
      litellmSettings: { drop_params: true, callbacks: [PLUGIN, upstream] },
    };
    expect(validateRoutingConfig(cfg, stage)).toEqual([
      'litellm_settings.callbacks must not register ' +
        upstream +
        '; the gateway wrapper replaces it',
    ]);
  });

  test.each([
    ['general_settings', true],
    ['general_settings', 'true'],
    ['litellm_settings', true],
    ['litellm_settings', 1],
  ] as const)('rejects %s.allow_client_side_credentials = %j', (section, value) => {
    const base = litellm(pair);
    const cfg: LiteLLMConfig =
      section === 'general_settings'
        ? {
            ...base,
            generalSettings: { ...base.generalSettings, allow_client_side_credentials: value },
          }
        : {
            ...base,
            litellmSettings: { ...base.litellmSettings, allow_client_side_credentials: value },
          };
    expect(validateRoutingConfig(cfg, stage)).toEqual([
      section +
        ".allow_client_side_credentials must be absent or false; it turns off LiteLLM's request-body guard",
    ]);
  });

  test('accepts allow_client_side_credentials: false, the LiteLLM default', () => {
    const base = litellm(pair);
    const cfg: LiteLLMConfig = {
      ...base,
      generalSettings: { ...base.generalSettings, allow_client_side_credentials: false },
    };
    expect(validateRoutingConfig(cfg, stage)).toEqual([]);
  });

  test.each([[['api_key']], [[]], [[{ api_base: '.*' }]]])(
    'rejects a deployment with configurable_clientside_auth_params %j',
    (params) => {
      const models = [
        ...pair.slice(0, 2),
        { ...pair[2], configurableClientsideAuthParams: params },
        pair[3],
      ];
      expect(validateRoutingConfig(litellm(models), stage)).toEqual([
        'Model ' +
          CAPABLE +
          ' must not set litellm_params.configurable_clientside_auth_params; it lets a request body override credentials',
      ]);
    },
  );

  test('callbacks may include other loggers alongside the wrapper', () => {
    const cfg: LiteLLMConfig = {
      ...litellm(pair),
      litellmSettings: { drop_params: true, callbacks: [PLUGIN, 'otel'] },
    };
    expect(validateRoutingConfig(cfg, stage)).toEqual([]);
  });

  test.each([
    'bedrock/global.anthropic.claude-opus-5-5',
    'bedrock/invoke/nvidia.nemotron-super-3-120b',
  ])('rejects %s, which does not use the Converse API', (bad) => {
    const models = [model('switchyard', bad), model('switchyard', EFFICIENT)];
    const problems = validateRoutingConfig(
      litellm([...models, model('capable', bad), model('efficient', EFFICIENT)]),
      stage,
    );
    expect(problems).toContain(
      'Model ' + bad + ' must use the Bedrock Converse API (bedrock/converse/<model id>)',
    );
  });
});
