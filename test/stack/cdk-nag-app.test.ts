import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { bedrockInvokeResources } from '../../lib/bedrock-policy';
import { loadGatewayConfig } from '../../lib/config';
import { GatewayStack } from '../../lib/gateway-stack';
import { loadLiteLLMConfig } from '../../lib/routing-config';

const ACCOUNT = '123456789012';
const REGION = 'us-east-1';
const STACK_NAME = 'SwitchyardGateway';

/** The feature flags and defaults that `cdk synth` reads from cdk.json. */
const CDK_JSON_CONTEXT = (
  JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'cdk.json'), 'utf8')) as {
    context: Record<string, unknown>;
  }
).context;

/** Context lookups seeded so the app synthesizes offline, as in CI. */
const LOOKUPS = {
  [`availability-zones:account=${ACCOUNT}:region=${REGION}`]: ['us-east-1a', 'us-east-1b'],
  [`hosted-zone:account=${ACCOUNT}:domainName=example.com:region=${REGION}`]: {
    Id: '/hostedzone/Z0000000000000000000A',
    Name: 'example.com.',
  },
};

/** The `-c` values a reader passes to `cdk deploy`. */
const variants: Record<string, Record<string, string>> = {
  'http-optin': { allowedCidr: '203.0.113.0/24', allowPlainHttp: 'true' },
  https: {
    allowedCidr: '203.0.113.0/24',
    domainName: 'llm-gateway.example.com',
    hostedZone: 'example.com',
  },
  'https-certarn': {
    allowedCidr: '203.0.113.0/24',
    domainName: 'llm-gateway.example.com',
    certificateArn:
      'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
  },
};

interface ValidationReport {
  pluginReports?: {
    pluginName?: string;
    violations?: { ruleName: string; description?: string }[];
  }[];
}

interface PolicyStatement {
  Action: string | string[];
  Resource: unknown;
}

/**
 * test/stack/cdk-nag.test.ts builds the stack without cdk.json. This suite
 * builds the app the way bin/app.ts does, with the cdk.json context that
 * `cdk synth` loads, so cdk-nag checks the template readers deploy. That
 * context enables @aws-cdk/core:enablePartitionLiterals, which renders ARNs
 * with a literal partition instead of the AWS::Partition token.
 */
describe.each(Object.entries(variants))('cdk-nag AwsSolutions on the app (%s)', (_name, cli) => {
  let outdir: string;
  let synthError: unknown;
  let awsSolutionsReports: NonNullable<ValidationReport['pluginReports']>;
  let template: { Resources: Record<string, { Type: string; Properties?: unknown }> };

  beforeAll(() => {
    outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'cdk-nag-app-'));
    const app = new cdk.App({ outdir, context: { ...CDK_JSON_CONTEXT, ...LOOKUPS, ...cli } });
    const stack = new GatewayStack(app, STACK_NAME, {
      config: loadGatewayConfig(app.node),
      env: { account: ACCOUNT, region: REGION },
      description:
        'LiteLLM gateway with the NVIDIA Switchyard routing plugin for Amazon Bedrock (sample code)',
    });
    cdk.Tags.of(stack).add('Project', 'switchyard-bedrock-gateway');
    cdk.Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));

    try {
      app.synth();
    } catch (error) {
      synthError = error;
    }

    const reportPath = path.join(outdir, 'validation-report.json');
    const report = fs.existsSync(reportPath)
      ? (JSON.parse(fs.readFileSync(reportPath, 'utf8')) as ValidationReport)
      : {};
    awsSolutionsReports = (report.pluginReports ?? []).filter(
      (p) => p.pluginName === 'AwsSolutions',
    );
    template = JSON.parse(
      fs.readFileSync(path.join(outdir, `${STACK_NAME}.template.json`), 'utf8'),
    ) as typeof template;
  });

  afterAll(() => {
    fs.rmSync(outdir, { recursive: true, force: true });
  });

  test('synthesizes without throwing', () => {
    expect(synthError).toBeUndefined();
  });

  // The report omits a plugin with no violations, so an empty list is a pass.
  test('leaves no unacknowledged AwsSolutions finding', () => {
    const violations = awsSolutionsReports
      .flatMap((p) => p.violations ?? [])
      .map((v) => `${v.ruleName}: ${v.description ?? ''}`);
    expect(violations).toEqual([]);
  });

  test('renders the Bedrock resources with a literal partition', () => {
    const statements = Object.values(template.Resources)
      .filter((r) => r.Type === 'AWS::IAM::Policy')
      .flatMap(
        (r) =>
          (r.Properties as { PolicyDocument: { Statement: PolicyStatement[] } }).PolicyDocument
            .Statement,
      )
      .filter((s) => [s.Action].flat().includes('bedrock:InvokeModel'));
    expect(statements).toHaveLength(1);
    const resources = [statements[0].Resource].flat();
    for (const resource of resources) expect(typeof resource).toBe('string');

    const expected = bedrockInvokeResources(
      loadLiteLLMConfig().models.map((m) => m.model),
      REGION,
      ACCOUNT,
      'aws',
    );
    expect(expected).toContainEqual(
      expect.stringMatching(/^arn:aws:bedrock:\*::foundation-model\/.+\*$/),
    );
    expect([...(resources as string[])].sort()).toEqual(expected);
  });
});
