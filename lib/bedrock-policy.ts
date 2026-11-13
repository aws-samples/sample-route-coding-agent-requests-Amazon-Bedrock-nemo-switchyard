/**
 * Derive the Amazon Bedrock IAM resources for the task role from the selected
 * profile. Foundation-model wildcard ARNs are scoped to the configured model ID
 * prefixes: the trailing `*` covers version suffixes and also matches any
 * longer model ID that starts with the same prefix.
 */

/** Geographic and global cross-Region inference profile prefixes. */
const PROFILE_PREFIXES: ReadonlySet<string> = new Set([
  'global',
  'us',
  'eu',
  'apac',
  'us-gov',
  'jp',
  'au',
]);

/** LiteLLM route selectors that may precede the model ID. */
const ROUTE_PREFIXES = ['converse/', 'invoke/', 'chat_completions/'];

export const BEDROCK_INVOKE_ACTIONS = [
  'bedrock:InvokeModel',
  'bedrock:InvokeModelWithResponseStream',
];

/**
 * Strip LiteLLM's `bedrock/` provider prefix and any route selector, returning
 * the identifier Amazon Bedrock receives.
 */
export function bedrockModelId(litellmModel: string): string {
  if (!litellmModel.startsWith('bedrock/')) {
    throw new Error(`not an Amazon Bedrock model: ${litellmModel}`);
  }
  let id = litellmModel.slice('bedrock/'.length);
  for (const prefix of ROUTE_PREFIXES) {
    if (id.startsWith(prefix)) {
      id = id.slice(prefix.length);
      break;
    }
  }
  if (id.length === 0 || id.includes('*') || id.includes('/')) {
    throw new Error(`unsupported Amazon Bedrock model identifier: ${litellmModel}`);
  }
  return id;
}

/** True when the identifier is a system-defined cross-Region inference profile. */
export function isInferenceProfile(modelId: string): boolean {
  const firstSegment = modelId.split('.')[0];
  return PROFILE_PREFIXES.has(firstSegment);
}

/**
 * IAM resource ARNs that allow invoking the given LiteLLM models.
 *
 * - Inference profiles (`global.`, `us.`, ...) need the profile ARN in the
 *   calling Region plus the underlying foundation model in every Region the
 *   profile may route to, hence the `*` Region and a trailing `*` that covers
 *   version suffixes such as `-v1:0`. That `*` also matches any other model ID
 *   with the same prefix, and the foundation-model grant has no
 *   `bedrock:InferenceProfileArn` condition, so the role can also invoke the
 *   base model directly.
 * - Bare model IDs are invoked in-Region through the foundation-model ARN.
 */
export function bedrockInvokeResources(
  litellmModels: readonly string[],
  region: string,
  account: string,
  partition = 'aws',
): string[] {
  const resources = new Set<string>();
  for (const litellmModel of litellmModels) {
    const id = bedrockModelId(litellmModel);
    if (isInferenceProfile(id)) {
      const base = id.slice(id.indexOf('.') + 1);
      resources.add(`arn:${partition}:bedrock:${region}:${account}:inference-profile/${id}`);
      resources.add(`arn:${partition}:bedrock:*::foundation-model/${base}*`);
    } else {
      resources.add(`arn:${partition}:bedrock:${region}::foundation-model/${id}`);
    }
  }
  return [...resources].sort();
}
