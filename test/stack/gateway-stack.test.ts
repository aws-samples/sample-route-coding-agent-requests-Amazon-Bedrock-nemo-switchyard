import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { bedrockInvokeResources } from '../../lib/bedrock-policy';
import type { GatewayConfig } from '../../lib/config';
import { GatewayStack } from '../../lib/gateway-stack';
import { loadLiteLLMConfig } from '../../lib/routing-config';

const ACCOUNT = '123456789012';
const REGION = 'us-east-1';

const commonConfig: GatewayConfig = {
  allowedCidr: '203.0.113.0/24',
  desiredCount: 1,
  cpuArchitecture: 'arm64',
};

/** Plain HTTP, only by the explicit opt-in. */
const baseConfig: GatewayConfig = { ...commonConfig, allowPlainHttp: true };

const httpsConfig: GatewayConfig = {
  ...commonConfig,
  domainName: 'llm-gateway.example.com',
  hostedZone: 'example.com',
};

const CERTIFICATE_ARN =
  'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555';

/** HTTPS with an existing ACM certificate and no Route 53 hosted zone. */
const certArnConfig: GatewayConfig = {
  ...commonConfig,
  domainName: 'llm-gateway.example.com',
  certificateArn: CERTIFICATE_ARN,
};

function synthStack(config: GatewayConfig = baseConfig): GatewayStack {
  const app = new cdk.App({
    context: {
      // Pre-seeded Route 53 lookup so the HTTPS variant synthesizes offline.
      [`hosted-zone:account=${ACCOUNT}:domainName=example.com:region=${REGION}`]: {
        Id: '/hostedzone/Z0000000000000000000A',
        Name: 'example.com.',
      },
    },
  });
  return new GatewayStack(app, 'TestStack', {
    config,
    env: { account: ACCOUNT, region: REGION },
  });
}

function synth(config: GatewayConfig = baseConfig): Template {
  return Template.fromStack(synthStack(config));
}

const API_PATHS = ['/v1/chat/completions', '/v1/models', '/health/liveliness'];

const NOT_FOUND_DEFAULT = [
  {
    Type: 'fixed-response',
    FixedResponseConfig: { StatusCode: '404', ContentType: 'text/plain', MessageBody: 'Not found' },
  },
];

interface PolicyStatement {
  Sid?: string;
  Effect: string;
  Action: string | string[];
  Resource: CfnValue | CfnValue[];
}

type CfnValue = string | { 'Fn::Join': [string, CfnValue[]] } | { Ref: string };

/** Render ARNs that CDK emits as Fn::Join over the AWS::Partition pseudo parameter. */
function renderArns(resource: CfnValue | CfnValue[]): string[] {
  const render = (v: CfnValue): string => {
    if (typeof v === 'string') return v;
    if ('Ref' in v) return v.Ref === 'AWS::Partition' ? 'aws' : `\${${v.Ref}}`;
    const [sep, parts] = v['Fn::Join'];
    return parts.map(render).join(sep);
  };
  return (Array.isArray(resource) ? resource : [resource]).map(render);
}

describe('GatewayStack network', () => {
  const template = synth();

  test('creates a two-AZ VPC with one NAT gateway and a Bedrock Runtime endpoint', () => {
    template.resourceCountIs('AWS::EC2::VPC', 1);
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
    template.resourceCountIs('AWS::EC2::Subnet', 4);
    template.hasResourceProperties('AWS::EC2::VPCEndpoint', {
      ServiceName: `com.amazonaws.${REGION}.bedrock-runtime`,
      VpcEndpointType: 'Interface',
      PrivateDnsEnabled: true,
    });
  });
});

describe('GatewayStack task role', () => {
  const template = synth();

  test('is assumable only by ECS tasks', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Action: 'sts:AssumeRole',
            Principal: { Service: 'ecs-tasks.amazonaws.com' },
          }),
        ],
      },
    });
  });

  test('may invoke exactly the configured Bedrock models', () => {
    const expected = bedrockInvokeResources(
      loadLiteLLMConfig().models.map((m) => m.model),
      REGION,
      ACCOUNT,
    );
    const policies = template.findResources('AWS::IAM::Policy');
    const statements = Object.values(policies).flatMap(
      (p) =>
        (p.Properties as { PolicyDocument: { Statement: PolicyStatement[] } }).PolicyDocument
          .Statement,
    );
    const bedrock = statements.find((s) => s.Sid === 'InvokeConfiguredBedrockModels');
    expect(bedrock).toBeDefined();
    expect(bedrock?.Effect).toBe('Allow');
    expect(bedrock?.Action).toEqual([
      'bedrock:InvokeModel',
      'bedrock:InvokeModelWithResponseStream',
    ]);
    expect(renderArns(bedrock?.Resource ?? []).sort()).toEqual(expected);
  });

  test('never grants a bare wildcard on models or profiles', () => {
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain('foundation-model/*"');
    expect(json).not.toContain('inference-profile/*"');
  });
});

describe('GatewayStack configuration guard', () => {
  test('refuses to synthesize when the routing files disagree', () => {
    const app = new cdk.App();
    expect(
      () =>
        new GatewayStack(app, 'Broken', {
          config: baseConfig,
          env: { account: ACCOUNT, region: REGION },
          litellmConfigPath: `${__dirname}/fixtures/litellm-missing-alias.yaml`,
        }),
    ).toThrow(/Routing configuration is inconsistent[\s\S]*Stage requires/);
  });

  test.each([
    ['litellm-allow-client-side-credentials.yaml', /allow_client_side_credentials must be absent/],
    ['litellm-clientside-auth-params.yaml', /must not set litellm_params\.configurable_clientside/],
  ])('refuses to synthesize when %s turns off the request-body guard', (fixture, problem) => {
    const app = new cdk.App();
    expect(
      () =>
        new GatewayStack(app, 'Unguarded', {
          config: baseConfig,
          env: { account: ACCOUNT, region: REGION },
          litellmConfigPath: `${__dirname}/fixtures/${fixture}`,
        }),
    ).toThrow(problem);
  });
});

interface ContainerDefinition {
  Name: string;
  Command: string[];
  Environment?: { Name: string; Value: unknown }[];
  Secrets?: { Name: string }[];
  PortMappings?: { ContainerPort: number }[];
  DependsOn?: { Condition: string; ContainerName: string }[];
  HealthCheck?: { Command: string[] };
  Essential?: boolean;
}

function containers(template: Template): Record<string, ContainerDefinition> {
  const [taskDef] = Object.values(template.findResources('AWS::ECS::TaskDefinition'));
  const defs = (taskDef.Properties as { ContainerDefinitions: ContainerDefinition[] })
    .ContainerDefinitions;
  return Object.fromEntries(defs.map((c) => [c.Name, c]));
}

describe('GatewayStack service', () => {
  const template = synth();
  const { litellm } = containers(template);

  test('runs one Fargate task definition with one plugin container on the chosen architecture', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      Cpu: '1024',
      Memory: '2048',
      NetworkMode: 'awsvpc',
      RequiresCompatibilities: ['FARGATE'],
      RuntimePlatform: { CpuArchitecture: 'ARM64', OperatingSystemFamily: 'LINUX' },
    });
    expect(Object.keys(containers(template)).sort()).toEqual(['litellm']);
    expect(litellm.Essential).toBe(true);
  });

  test('honors cpuArchitecture=x86_64', () => {
    synth({ ...baseConfig, cpuArchitecture: 'x86_64' }).hasResourceProperties(
      'AWS::ECS::TaskDefinition',
      { RuntimePlatform: { CpuArchitecture: 'X86_64', OperatingSystemFamily: 'LINUX' } },
    );
  });

  test('LiteLLM exposes the ALB target port', () => {
    expect(litellm.PortMappings).toEqual([expect.objectContaining({ ContainerPort: 4000 })]);
    expect(litellm.DependsOn).toBeUndefined();
  });

  test('the master key is injected as a secret, never as plain environment', () => {
    expect(litellm.Secrets?.map((s) => s.Name)).toEqual(['LITELLM_MASTER_KEY_SUFFIX']);
    const envNames = litellm.Environment?.map((e) => e.Name) ?? [];
    expect(envNames).not.toContain('LITELLM_MASTER_KEY');
    expect(envNames).toEqual(
      expect.arrayContaining(['LITELLM_CONFIG_TEXT', 'SWITCHYARD_CONFIG_TEXT', 'AWS_REGION']),
    );
    expect(JSON.stringify(template.toJSON())).not.toMatch(/sk-[A-Za-z0-9]{20,}/);
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      GenerateSecretString: { ExcludePunctuation: true, PasswordLength: 48 },
    });
  });

  test('LiteLLM turns off its API docs routes and sets the admin UI switch', () => {
    const env = Object.fromEntries(
      (litellm.Environment ?? []).map((e) => [e.Name, e.Value as string]),
    );
    expect(env).toEqual(
      expect.objectContaining({
        DISABLE_ADMIN_UI: 'True',
        NO_DOCS: 'True',
        NO_REDOC: 'True',
        NO_OPENAPI: 'True',
      }),
    );
  });

  test('LiteLLM uses the cost map bundled in the image', () => {
    const env = Object.fromEntries(
      (litellm.Environment ?? []).map((e) => [e.Name, e.Value as string]),
    );
    expect(env.LITELLM_LOCAL_MODEL_COST_MAP).toBe('True');
  });

  test('the plugin container receives both profile files', () => {
    const env = (name: string) =>
      litellm.Environment?.find((e) => e.Name === name)?.Value as string;
    expect(env('LITELLM_CONFIG_TEXT')).toContain('model_list:');
    expect(env('LITELLM_CONFIG_TEXT')).toContain('master_key: os.environ/LITELLM_MASTER_KEY');
    expect(env('SWITCHYARD_CONFIG_TEXT')).toContain('algorithm = "stage"');
  });

  test('LiteLLM is the load balancer target with a local health check', () => {
    expect(litellm.HealthCheck?.Command[1]).toContain('http://127.0.0.1:4000/health/liveliness');
    template.hasResourceProperties('AWS::ECS::Service', {
      LoadBalancers: [Match.objectLike({ ContainerName: 'litellm', ContainerPort: 4000 })],
      LaunchType: 'FARGATE',
      DesiredCount: 1,
      DeploymentConfiguration: Match.objectLike({
        DeploymentCircuitBreaker: { Enable: true, Rollback: true },
        MinimumHealthyPercent: 100,
      }),
      NetworkConfiguration: {
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: 'DISABLED' }),
      },
    });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      HealthCheckPath: '/health/liveliness',
      TargetGroupAttributes: Match.arrayWith([
        { Key: 'deregistration_delay.timeout_seconds', Value: '30' },
      ]),
    });
  });

  test('the load balancer idle timeout covers long agent turns', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internet-facing',
      LoadBalancerAttributes: Match.arrayWith([
        { Key: 'idle_timeout.timeout_seconds', Value: '600' },
      ]),
    });
  });

  test('a server migration retains the existing Switchyard log group', () => {
    const migrated = synth({ ...baseConfig, retainLegacyLogs: true });
    const groups = migrated.findResources('AWS::Logs::LogGroup');
    const legacy = Object.entries(groups).find(([id]) =>
      id.startsWith('GatewaySwitchyardLogs'),
    )?.[1];
    expect(legacy).toEqual(
      expect.objectContaining({ DeletionPolicy: 'Retain', UpdateReplacePolicy: 'Retain' }),
    );
    expect(Object.keys(containers(migrated))).toEqual(['litellm']);
  });

  test('log groups are explicit with a retention period', () => {
    template.resourceCountIs('AWS::Logs::LogGroup', 1);
    template.allResourcesProperties('AWS::Logs::LogGroup', { RetentionInDays: 30 });
    expect(
      template.findResources('AWS::Lambda::Function', {
        Properties: { Description: Match.stringLikeRegexp('LogRetention') },
      }),
    ).toEqual({});
  });

  test('exposes the outputs the README and smoke test rely on', () => {
    // Exact keys: scripts/smoke-test.sh queries them with OutputKey=='...'.
    const all = template.toJSON().Outputs as Record<string, { Value: CfnValue }>;
    for (const name of [
      'GatewayUrl',
      'MasterKeySecretArn',
      'MasterKeyCommand',
      'SmokeTestCommand',
      'ClusterName',
      'ServiceName',
    ]) {
      expect(all).toHaveProperty(name);
    }
    // The README's key rotation forces a new deployment of exactly this service.
    const clusters = Object.keys(template.findResources('AWS::ECS::Cluster'));
    const services = Object.keys(template.findResources('AWS::ECS::Service'));
    expect(clusters).toHaveLength(1);
    expect(services).toHaveLength(1);
    expect(all.ClusterName.Value).toEqual({ Ref: clusters[0] });
    expect(all.ServiceName.Value).toEqual({ 'Fn::GetAtt': [services[0], 'Name'] });
    const [rendered] = renderArns(all.MasterKeyCommand.Value);
    expect(rendered).toMatch(
      /^echo "sk-\$\(aws secretsmanager get-secret-value --region us-east-1 --secret-id \$\{.+\} --query SecretString --output text\)"$/,
    );
  });
});

describe('GatewayStack ingress', () => {
  test('with allowPlainHttp: HTTP listener on 80, open only to allowedCidr', () => {
    const template = synth();
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 1);
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      Protocol: 'HTTP',
    });
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: Match.stringLikeRegexp('ELB'),
      SecurityGroupIngress: [
        Match.objectLike({ CidrIp: '203.0.113.0/24', FromPort: 80, ToPort: 80, IpProtocol: 'tcp' }),
      ],
    });
  });

  test('with a domain: HTTPS on 443 with an ACM certificate, HTTP redirect, and a Route 53 alias', () => {
    const template = synth(httpsConfig);
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 2);
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 443,
      Protocol: 'HTTPS',
    });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      DefaultActions: [Match.objectLike({ Type: 'redirect' })],
    });
    template.hasResourceProperties('AWS::CertificateManager::Certificate', {
      DomainName: 'llm-gateway.example.com',
      ValidationMethod: 'DNS',
    });
    template.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: 'llm-gateway.example.com.',
      Type: 'A',
    });
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: Match.stringLikeRegexp('ELB'),
      SecurityGroupIngress: Match.arrayWith([
        Match.objectLike({ CidrIp: '203.0.113.0/24', FromPort: 443, ToPort: 443 }),
        Match.objectLike({ CidrIp: '203.0.113.0/24', FromPort: 80, ToPort: 80 }),
      ]),
    });
  });

  test('with an existing certificate: HTTPS on 443 with that ARN, HTTP redirect, and no certificate or DNS record', () => {
    const template = synth(certArnConfig);
    template.resourceCountIs('AWS::ElasticLoadBalancingV2::Listener', 2);
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 443,
      Protocol: 'HTTPS',
      Certificates: [{ CertificateArn: CERTIFICATE_ARN }],
    });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      DefaultActions: [Match.objectLike({ Type: 'redirect' })],
    });
    template.resourceCountIs('AWS::CertificateManager::Certificate', 0);
    template.resourceCountIs('AWS::Route53::RecordSet', 0);
    template.hasResourceProperties('AWS::EC2::SecurityGroup', {
      GroupDescription: Match.stringLikeRegexp('ELB'),
      SecurityGroupIngress: [
        Match.objectLike({ CidrIp: '203.0.113.0/24', FromPort: 443, ToPort: 443 }),
        Match.objectLike({ CidrIp: '203.0.113.0/24', FromPort: 80, ToPort: 80 }),
      ],
    });
    const outputs = template.toJSON().Outputs as Record<
      string,
      { Value: unknown; Description: string }
    >;
    expect(outputs.GatewayUrl.Value).toBe('https://llm-gateway.example.com');
    expect(outputs.LoadBalancerDnsName.Description).toMatch(/llm-gateway\.example\.com.*CNAME/);
    expect(synth(httpsConfig).toJSON().Outputs).not.toHaveProperty('LoadBalancerDnsName');
  });

  test('refuses to synthesize without HTTPS settings or the plain-HTTP opt-in', () => {
    expect(() => synthStack(commonConfig)).toThrow(/allowPlainHttp=true/);
  });

  test('refuses a certificate from another Region', () => {
    expect(() =>
      synthStack({
        ...certArnConfig,
        certificateArn: CERTIFICATE_ARN.replace('us-east-1', 'eu-west-1'),
      }),
    ).toThrow(/certificateArn is in eu-west-1/);
  });

  test.each([
    ['HTTP', baseConfig, 80],
    ['HTTPS', httpsConfig, 443],
    ['HTTPS with an existing certificate', certArnConfig, 443],
  ] as const)(
    '%s listener answers 404 by default and forwards only the API paths',
    (_name, config, port) => {
      const template = synth(config);
      const listeners = Object.values(
        template.findResources('AWS::ElasticLoadBalancingV2::Listener', {
          Properties: { Port: port },
        }),
      );
      expect(listeners).toHaveLength(1);
      const listener = listeners[0].Properties as { DefaultActions: unknown };
      expect(listener.DefaultActions).toEqual(NOT_FOUND_DEFAULT);

      const rules = Object.values(
        template.findResources('AWS::ElasticLoadBalancingV2::ListenerRule'),
      );
      expect(rules).toHaveLength(1);
      const rule = rules[0].Properties as {
        Priority: number;
        Conditions: { Field: string; PathPatternConfig: { Values: string[] } }[];
        Actions: unknown[];
      };
      expect(rule.Priority).toBe(10);
      expect(rule.Conditions).toEqual([
        { Field: 'path-pattern', PathPatternConfig: { Values: API_PATHS } },
      ]);
      const targetGroups = Object.keys(
        template.findResources('AWS::ElasticLoadBalancingV2::TargetGroup'),
      );
      expect(targetGroups).toHaveLength(1);
      expect(rule.Actions).toEqual([
        expect.objectContaining({ Type: 'forward', TargetGroupArn: { Ref: targetGroups[0] } }),
      ]);

      if (port === 443) {
        const redirects = Object.values(
          template.findResources('AWS::ElasticLoadBalancingV2::Listener', {
            Properties: { Port: 80 },
          }),
        );
        expect(redirects).toHaveLength(1);
        const redirect = redirects[0].Properties as { DefaultActions: unknown };
        expect(redirect.DefaultActions).toEqual([
          {
            Type: 'redirect',
            RedirectConfig: { Port: '443', Protocol: 'HTTPS', StatusCode: 'HTTP_301' },
          },
        ]);
      }
    },
  );

  test('with allowPlainHttp: one synth warning and the GatewayUrl description flag plain HTTP', () => {
    const stack = synthStack(baseConfig);
    const warnings = Annotations.fromStack(stack).findWarning('*', Match.anyValue());
    expect(warnings).toHaveLength(1);
    expect(warnings[0].entry.data).toMatch(/plain HTTP/);
    const outputs = Template.fromStack(stack).toJSON().Outputs as Record<
      string,
      { Description: string }
    >;
    expect(outputs.GatewayUrl.Description).toMatch(/plain HTTP/);
  });

  test.each([httpsConfig, certArnConfig])(
    'with HTTPS: synthesizes without warning annotations',
    (config) => {
      const stack = synthStack(config);
      expect(Annotations.fromStack(stack).findWarning('*', Match.anyValue())).toEqual([]);
      const outputs = Template.fromStack(stack).toJSON().Outputs as Record<
        string,
        { Description: string }
      >;
      expect(outputs.GatewayUrl.Description).not.toMatch(/plain HTTP/);
    },
  );

  test.each([baseConfig, httpsConfig, certArnConfig])(
    'the load balancer idle timeout is 600 seconds',
    (config) => {
      synth(config).hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
        LoadBalancerAttributes: Match.arrayWith([
          { Key: 'idle_timeout.timeout_seconds', Value: '600' },
        ]),
      });
    },
  );

  test.each([baseConfig, httpsConfig, certArnConfig])(
    'the load balancer drops invalid header fields',
    (config) => {
      synth(config).hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
        LoadBalancerAttributes: Match.arrayWith([
          { Key: 'routing.http.drop_invalid_header_fields.enabled', Value: 'true' },
        ]),
      });
    },
  );

  test.each([baseConfig, httpsConfig, certArnConfig])(
    'never opens a security group ingress to the world',
    (config) => {
      const template = synth(config);
      interface IngressRule {
        CidrIp?: string;
        CidrIpv6?: string;
      }
      const inline = Object.values(template.findResources('AWS::EC2::SecurityGroup')).flatMap(
        (sg) =>
          (sg.Properties as { SecurityGroupIngress?: IngressRule[] }).SecurityGroupIngress ?? [],
      );
      const standalone = Object.values(
        template.findResources('AWS::EC2::SecurityGroupIngress'),
      ).map((r) => r.Properties as IngressRule);
      const rules = [...inline, ...standalone];
      expect(rules.length).toBeGreaterThan(0);
      for (const rule of rules) {
        expect(rule.CidrIp).not.toBe('0.0.0.0/0');
        expect(rule.CidrIpv6).not.toBe('::/0');
      }
    },
  );
});

describe('GatewayStack WAF', () => {
  const template = synth();

  test('attaches a regional web ACL with reputation, known-bad-inputs, and rate-limit rules', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'REGIONAL',
      DefaultAction: { Allow: {} },
      Rules: [
        Match.objectLike({
          Name: 'AWSManagedRulesAmazonIpReputationList',
          Priority: 0,
          Statement: {
            ManagedRuleGroupStatement: {
              VendorName: 'AWS',
              Name: 'AWSManagedRulesAmazonIpReputationList',
            },
          },
        }),
        Match.objectLike({
          Name: 'AWSManagedRulesKnownBadInputsRuleSet',
          Priority: 1,
        }),
        Match.objectLike({
          Name: 'RateLimitPerIp',
          Priority: 2,
          Action: { Block: {} },
          Statement: { RateBasedStatement: { Limit: 2000, AggregateKeyType: 'IP' } },
        }),
      ],
    });
    template.hasResourceProperties('AWS::WAFv2::WebACLAssociation', {
      ResourceArn: { Ref: Match.stringLikeRegexp('GatewayServiceLB') },
    });
  });

  test('does not use the generic body rule set that blocks agent payloads', () => {
    expect(JSON.stringify(template.toJSON())).not.toContain('AWSManagedRulesCommonRuleSet');
  });

  test('keeps no sampled requests, which would store the Authorization header', () => {
    interface VisibilityConfig {
      SampledRequestsEnabled: boolean;
      CloudWatchMetricsEnabled: boolean;
    }
    const acls = Object.values(template.findResources('AWS::WAFv2::WebACL'));
    expect(acls).toHaveLength(1);
    const acl = acls[0].Properties as {
      VisibilityConfig: VisibilityConfig;
      Rules: { VisibilityConfig: VisibilityConfig }[];
    };
    expect(acl.Rules).toHaveLength(3);
    for (const config of [acl.VisibilityConfig, ...acl.Rules.map((r) => r.VisibilityConfig)]) {
      expect(config).toEqual(
        expect.objectContaining({ SampledRequestsEnabled: false, CloudWatchMetricsEnabled: true }),
      );
    }
  });
});
