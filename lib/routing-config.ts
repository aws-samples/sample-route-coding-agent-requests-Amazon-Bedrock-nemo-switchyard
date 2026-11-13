import * as fs from 'node:fs';
import * as path from 'node:path';
import { parse as parseToml } from 'smol-toml';
import { parse as parseYaml } from 'yaml';
import { bedrockModelId } from './bedrock-policy';

export const PROFILES_DIR = path.resolve(__dirname, '..', 'profiles');
// The gateway wrapper applies Switchyard to the routed group and passes
// single-model baseline groups through (docker/gateway/switchyard_gateway).
export const PLUGIN = 'switchyard_gateway.configured.ROUTING_PLUGIN';
// Upstream's configured plugin. Registering it beside the wrapper loads a
// second, unwrapped plugin instance from the same TOML.
const UPSTREAM_PLUGIN = 'switchyard_litellm.configuration.configured_plugin.ROUTING_PLUGIN';
// The public group Switchyard routes. Every other group is a one-model baseline.
export const ROUTED_GROUP = 'switchyard';
const CONVERSE_PREFIX = 'bedrock/converse/';
export function profileDirectory(name = 'stage'): string {
  if (!/^[a-z][a-z0-9-]*$/.test(name))
    throw new Error('routingProfile must be a profile directory name');
  return path.join(PROFILES_DIR, name);
}
// LiteLLM reads these keys only here: the proxy-wide opt-in from
// general_settings, the per-deployment one from litellm_params. Each turns
// off LiteLLM's request-body guard, so a profile may set neither.
const CLIENT_SIDE_CREDENTIALS = 'allow_client_side_credentials';
const CLIENT_SIDE_AUTH_PARAMS = 'configurable_clientside_auth_params';
export interface LiteLLMModel {
  readonly modelName: string;
  readonly model: string;
  readonly awsRegionName: unknown;
  readonly configurableClientsideAuthParams?: unknown;
}
export interface LiteLLMConfig {
  readonly models: readonly LiteLLMModel[];
  readonly routerSettings: Readonly<Record<string, unknown>>;
  readonly litellmSettings: Readonly<Record<string, unknown>>;
  readonly generalSettings: Readonly<Record<string, unknown>>;
  readonly text: string;
}
export interface SwitchyardConfig {
  readonly algorithm: string;
  readonly settings: Readonly<Record<string, unknown>>;
  readonly text: string;
}
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function string(value: unknown, where: string): string {
  if (typeof value !== 'string' || !value.trim())
    throw new Error(where + ' must be a nonempty string');
  return value;
}
export function loadLiteLLMConfig(
  filePath = path.join(profileDirectory(), 'litellm.yaml'),
): LiteLLMConfig {
  const text = fs.readFileSync(filePath, 'utf8');
  const doc: unknown = parseYaml(text);
  if (!record(doc) || !Array.isArray(doc.model_list) || !doc.model_list.length)
    throw new Error(filePath + ': expected a nonempty model_list');
  const models = doc.model_list.map((entry: unknown) => {
    if (!record(entry) || !record(entry.litellm_params))
      throw new Error(filePath + ': each model needs litellm_params');
    return {
      modelName: string(entry.model_name, 'model_name'),
      model: string(entry.litellm_params.model, 'litellm_params.model'),
      awsRegionName: entry.litellm_params.aws_region_name,
      configurableClientsideAuthParams: entry.litellm_params[CLIENT_SIDE_AUTH_PARAMS],
    };
  });
  return {
    models,
    routerSettings: record(doc.router_settings) ? doc.router_settings : {},
    litellmSettings: record(doc.litellm_settings) ? doc.litellm_settings : {},
    generalSettings: record(doc.general_settings) ? doc.general_settings : {},
    text,
  };
}
export function loadSwitchyardConfig(
  filePath = path.join(profileDirectory(), 'switchyard.toml'),
): SwitchyardConfig {
  const text = fs.readFileSync(filePath, 'utf8');
  const doc: unknown = parseToml(text);
  if (!record(doc)) throw new Error(filePath + ': expected TOML settings');
  return { algorithm: string(doc.algorithm, 'algorithm'), settings: doc, text };
}
/** Model strings in a group, in declaration order, without repeats. */
export function groupModels(litellm: LiteLLMConfig, group: string): string[] {
  return [...new Set(litellm.models.filter((m) => m.modelName === group).map((m) => m.model))];
}
function validateGroups(litellm: LiteLLMConfig): string[] {
  const problems: string[] = [];
  const routed = groupModels(litellm, ROUTED_GROUP);
  if (!routed.length) problems.push('There is no "' + ROUTED_GROUP + '" model group to route');
  const baselines = new Map<string, string[]>();
  for (const m of litellm.models) {
    if (m.modelName !== ROUTED_GROUP)
      baselines.set(m.modelName, [...(baselines.get(m.modelName) ?? []), m.model]);
  }
  for (const [group, models] of baselines) {
    if (models.length !== 1)
      problems.push(
        `Baseline group ${group} has ${models.length} deployments; only "${ROUTED_GROUP}" may have more than one`,
      );
  }
  const baselineModels = new Set([...baselines.values()].flat());
  for (const model of routed) {
    if (!baselineModels.has(model))
      problems.push(`Routed model ${model} has no baseline group; add a one-model group for it`);
  }
  return problems;
}
export function validateRoutingConfig(
  litellm: LiteLLMConfig,
  switchyard: SwitchyardConfig,
): string[] {
  const problems: string[] = [];
  problems.push(...validateGroups(litellm));
  for (const model of litellm.models) {
    if (!model.model.startsWith(CONVERSE_PREFIX))
      problems.push(
        `Model ${model.model} must use the Bedrock Converse API (${CONVERSE_PREFIX}<model id>)`,
      );
    try {
      bedrockModelId(model.model);
    } catch {
      problems.push('Unsupported Bedrock model: ' + model.model);
    }
    if (model.awsRegionName !== 'os.environ/AWS_REGION')
      problems.push('Each model must use aws_region_name: os.environ/AWS_REGION');
    if (model.configurableClientsideAuthParams != null)
      problems.push(
        `Model ${model.model} must not set litellm_params.${CLIENT_SIDE_AUTH_PARAMS}; it lets a request body override credentials`,
      );
  }
  if (litellm.generalSettings.master_key !== 'os.environ/LITELLM_MASTER_KEY')
    problems.push('general_settings.master_key must be os.environ/LITELLM_MASTER_KEY');
  for (const [section, settings] of [
    ['general_settings', litellm.generalSettings],
    ['litellm_settings', litellm.litellmSettings],
  ] as const) {
    const value = settings[CLIENT_SIDE_CREDENTIALS];
    if (value !== undefined && value !== false)
      problems.push(
        `${section}.${CLIENT_SIDE_CREDENTIALS} must be absent or false; it turns off LiteLLM's request-body guard`,
      );
  }
  if (litellm.litellmSettings.drop_params !== true) problems.push('drop_params must be true');
  const plugins = litellm.routerSettings.plugins;
  if (!Array.isArray(plugins) || plugins.length !== 1 || plugins[0] !== PLUGIN)
    problems.push('router_settings.plugins must be exactly [' + PLUGIN + ']');
  const callbacks = litellm.litellmSettings.callbacks;
  if (!Array.isArray(callbacks) || !callbacks.includes(PLUGIN))
    problems.push('litellm_settings.callbacks must register ' + PLUGIN);
  if (Array.isArray(callbacks) && callbacks.includes(UPSTREAM_PLUGIN))
    problems.push(
      'litellm_settings.callbacks must not register ' +
        UPSTREAM_PLUGIN +
        '; the gateway wrapper replaces it',
    );
  const settings = switchyard.settings;
  const candidates = groupModels(litellm, ROUTED_GROUP).length;
  const algorithm = switchyard.algorithm;
  const stageKeys = [
    'algorithm',
    'picker',
    'confidence_threshold',
    'recent_window',
    'only_on_wrong_signal_escalation',
    'escalation_note',
    'deescalation_note',
    'capable_system_prompt',
    'efficient_system_prompt',
  ];
  const randomKeys = ['algorithm', 'weights', 'seed'];
  if (algorithm !== 'stage' && algorithm !== 'random') {
    problems.push('algorithm must be stage or random');
    return problems;
  }
  for (const key of Object.keys(settings)) {
    if (!(algorithm === 'stage' ? stageKeys : randomKeys).includes(key))
      problems.push('Unknown ' + algorithm + ' setting: ' + key);
  }
  if (algorithm === 'stage') {
    if (candidates !== 2)
      problems.push(
        `Stage requires exactly two unique candidates in "${ROUTED_GROUP}", capable first and efficient second`,
      );
    if (!['efficient_first', 'capable_first'].includes(String(settings.picker)))
      problems.push('Stage picker must be efficient_first or capable_first');
    const threshold = settings.confidence_threshold;
    if (
      typeof threshold !== 'number' ||
      !Number.isFinite(threshold) ||
      threshold < 0 ||
      threshold > 1
    )
      problems.push('confidence_threshold must be between 0 and 1');
    if (
      settings.recent_window !== undefined &&
      (typeof settings.recent_window !== 'number' ||
        !Number.isInteger(settings.recent_window) ||
        settings.recent_window < 0)
    )
      problems.push('recent_window must be a nonnegative integer');
    if (
      settings.only_on_wrong_signal_escalation !== undefined &&
      typeof settings.only_on_wrong_signal_escalation !== 'boolean'
    )
      problems.push('only_on_wrong_signal_escalation must be Boolean');
    for (const key of [
      'escalation_note',
      'deescalation_note',
      'capable_system_prompt',
      'efficient_system_prompt',
    ]) {
      if (
        settings[key] !== undefined &&
        (typeof settings[key] !== 'string' || !settings[key].trim())
      )
        problems.push(key + ' must be nonempty text');
    }
    if (settings.deescalation_note !== undefined && settings.escalation_note === undefined)
      problems.push('deescalation_note requires escalation_note');
  } else {
    if (candidates < 2)
      problems.push(`Random requires at least two unique candidates in "${ROUTED_GROUP}"`);
    const weights = settings.weights;
    if (
      weights !== undefined &&
      (!Array.isArray(weights) ||
        weights.length !== candidates ||
        weights.some((w) => typeof w !== 'number' || !Number.isFinite(w) || w < 0) ||
        !weights.some((w) => typeof w === 'number' && w > 0))
    )
      problems.push(
        'Random weights must match unique candidates and be finite, nonnegative, with at least one positive value',
      );
    if (
      settings.seed !== undefined &&
      (typeof settings.seed !== 'number' ||
        !Number.isSafeInteger(settings.seed) ||
        settings.seed < 0)
    )
      problems.push('seed must be a nonnegative safe integer');
  }
  return problems;
}
