import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import type { GatewayConfig } from '../../lib/config';
import { GatewayStack } from '../../lib/gateway-stack';

const ACCOUNT = '123456789012';
const REGION = 'us-east-1';

const configs: Record<string, GatewayConfig> = {
  'http-optin': {
    allowedCidr: '203.0.113.0/24',
    desiredCount: 1,
    cpuArchitecture: 'arm64',
    allowPlainHttp: true,
  },
  https: {
    allowedCidr: '203.0.113.0/24',
    desiredCount: 2,
    cpuArchitecture: 'x86_64',
    domainName: 'llm-gateway.example.com',
    hostedZone: 'example.com',
  },
  'https-certarn': {
    allowedCidr: '203.0.113.0/24',
    desiredCount: 1,
    cpuArchitecture: 'arm64',
    domainName: 'llm-gateway.example.com',
    certificateArn:
      'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
  },
};

interface ValidationReport {
  pluginReports?: {
    pluginName?: string;
    violations?: { ruleName: string; severity?: string; description?: string }[];
  }[];
}

/**
 * cdk-nag v3 plugs into CDK's policy validation framework: `app.synth()` throws
 * when any unacknowledged finding remains, and the details land in
 * validation-report.json inside the cloud assembly.
 */
describe.each(Object.entries(configs))('cdk-nag AwsSolutions (%s)', (_name, config) => {
  test('synthesizes with no unacknowledged findings', () => {
    const outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'cdk-nag-'));
    const app = new cdk.App({
      outdir,
      context: {
        [`hosted-zone:account=${ACCOUNT}:domainName=example.com:region=${REGION}`]: {
          Id: '/hostedzone/Z0000000000000000000A',
          Name: 'example.com.',
        },
      },
    });
    new GatewayStack(app, 'NagStack', { config, env: { account: ACCOUNT, region: REGION } });
    cdk.Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));

    let synthError: unknown;
    try {
      app.synth();
    } catch (error) {
      synthError = error;
    }

    const reportPath = path.join(outdir, 'validation-report.json');
    const violations = fs.existsSync(reportPath)
      ? ((JSON.parse(fs.readFileSync(reportPath, 'utf8')) as ValidationReport).pluginReports ?? [])
          .flatMap((p) => p.violations ?? [])
          .map((v) => `${v.ruleName}: ${v.description ?? ''}`)
      : [];
    fs.rmSync(outdir, { recursive: true, force: true });

    expect(violations).toEqual([]);
    expect(synthError).toBeUndefined();
  });
});
