import {
  bedrockInvokeResources,
  bedrockModelId,
  isInferenceProfile,
} from '../../lib/bedrock-policy';
import { loadLiteLLMConfig } from '../../lib/routing-config';

describe('bedrockModelId', () => {
  test('strips the provider prefix and any route selector', () => {
    expect(bedrockModelId('bedrock/global.anthropic.claude-opus-5-5')).toBe(
      'global.anthropic.claude-opus-5-5',
    );
    expect(bedrockModelId('bedrock/converse/qwen.qwen3-coder-next')).toBe('qwen.qwen3-coder-next');
    expect(bedrockModelId('bedrock/invoke/anthropic.claude-v2')).toBe('anthropic.claude-v2');
    expect(bedrockModelId('bedrock/chat_completions/openai.gpt-oss-20b-1:0')).toBe(
      'openai.gpt-oss-20b-1:0',
    );
    expect(bedrockModelId('bedrock/converse/global.moonshotai.kimi-k3')).toBe(
      'global.moonshotai.kimi-k3',
    );
  });

  test('rejects non-Bedrock models, wildcards, and region paths', () => {
    expect(() => bedrockModelId('openai/gpt-4o')).toThrow(/not an Amazon Bedrock model/);
    expect(() => bedrockModelId('bedrock/*')).toThrow(/unsupported/);
    expect(() =>
      bedrockModelId('bedrock/chat_completions/us-gov-west-1/openai.gpt-oss-20b-1:0'),
    ).toThrow(/unsupported/);
  });
});

describe('isInferenceProfile', () => {
  test.each(['global.anthropic.x', 'us.anthropic.x', 'eu.amazon.nova', 'apac.x.y', 'us-gov.x.y'])(
    'recognizes %s',
    (id) => expect(isInferenceProfile(id)).toBe(true),
  );
  test.each(['anthropic.claude-v2', 'openai.gpt-oss-120b-1:0', 'qwen.qwen3-coder-next'])(
    'treats %s as a bare model',
    (id) => expect(isInferenceProfile(id)).toBe(false),
  );
});

describe('bedrockInvokeResources', () => {
  const region = 'us-east-1';
  const account = '123456789012';

  test('a global profile grants the profile in-Region and the base model in every Region', () => {
    expect(
      bedrockInvokeResources(['bedrock/global.anthropic.claude-opus-5-5'], region, account),
    ).toEqual([
      'arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5*',
      'arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-opus-5-5',
    ]);
  });

  test('a us profile with a version suffix is handled the same way', () => {
    expect(
      bedrockInvokeResources(
        ['bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0'],
        region,
        account,
      ),
    ).toEqual([
      'arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0*',
      'arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0',
    ]);
  });

  test('a bare model through the converse route is granted in-Region only', () => {
    expect(
      bedrockInvokeResources(['bedrock/converse/qwen.qwen3-coder-next'], region, account),
    ).toEqual(['arn:aws:bedrock:us-east-1::foundation-model/qwen.qwen3-coder-next']);
    expect(
      bedrockInvokeResources(['bedrock/converse/openai.gpt-oss-120b-1:0'], region, account),
    ).toEqual(['arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-120b-1:0']);
  });

  test('deduplicates, sorts, and honors the partition', () => {
    const out = bedrockInvokeResources(
      ['bedrock/global.a.b', 'bedrock/converse/global.a.b', 'bedrock/c.d'],
      'us-gov-west-1',
      account,
      'aws-us-gov',
    );
    expect(out).toEqual(
      [
        'arn:aws-us-gov:bedrock:*::foundation-model/a.b*',
        'arn:aws-us-gov:bedrock:us-gov-west-1::foundation-model/c.d',
        'arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:inference-profile/global.a.b',
      ].sort(),
    );
    expect(out).toHaveLength(3);
  });

  test('never produces a bare wildcard resource for the checked-in model list', () => {
    const models = loadLiteLLMConfig().models.map((m) => m.model);
    const out = bedrockInvokeResources(models, region, account);
    expect(out.length).toBeGreaterThan(0);
    for (const arn of out) {
      expect(arn).not.toMatch(/foundation-model\/\*$/);
      expect(arn).not.toMatch(/inference-profile\/\*$/);
    }
  });
});
