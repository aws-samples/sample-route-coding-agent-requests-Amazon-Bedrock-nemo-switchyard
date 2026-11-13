import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecsPatterns from 'aws-cdk-lib/aws-ecs-patterns';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { ConfigValidationError, LISTENER_HINT, type GatewayConfig } from './config';

/** Port LiteLLM listens on, reachable through the ALB security group. */
export const LITELLM_PORT = 4000;
/** End-to-end idle timeout. Long agent turns need more than the 60 s default. */
export const IDLE_TIMEOUT = cdk.Duration.minutes(10);
/**
 * Paths the load balancer forwards to LiteLLM. LiteLLM also serves an admin
 * UI, API docs, and key and model management routes; the load balancer answers
 * every other path with 404 so none of them is reachable.
 */
export const ALLOWED_PATHS = ['/v1/chat/completions', '/v1/models', '/health/liveliness'];
/**
 * LiteLLM switches set inside the container, behind the load balancer
 * allowlist. NO_DOCS, NO_REDOC, and NO_OPENAPI turn off the Swagger, ReDoc, and
 * OpenAPI routes. DISABLE_ADMIN_UI turns off the UI login backends, but LiteLLM
 * 1.102.2 still serves the /ui static files; ALLOWED_PATHS is what blocks /ui.
 */
export const LITELLM_SURFACE_FLAGS = {
  DISABLE_ADMIN_UI: 'True',
  NO_DOCS: 'True',
  NO_REDOC: 'True',
  NO_OPENAPI: 'True',
} as const;

export interface GatewayServiceProps {
  readonly vpc: ec2.IVpc;
  readonly config: GatewayConfig;
  /** Task role with permission to invoke the configured Bedrock models. */
  readonly taskRole: iam.IRole;
  /** Full text of the selected plugin switchyard.toml, written to the container at start. */
  readonly switchyardConfigText: string;
  /** Full text of the selected litellm.yaml, written to the container at start. */
  readonly litellmConfigText: string;
}

/** One Fargate task runs LiteLLM with the embedded Switchyard plugin. */
export class GatewayService extends Construct {
  public readonly cluster: ecs.Cluster;
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  public readonly service: ecsPatterns.ApplicationLoadBalancedFargateService;
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly masterKeySecret: secretsmanager.Secret;
  /** `https://host` when a domain is configured, else `http://<alb-dns>`. */
  public readonly gatewayUrl: string;

  constructor(scope: Construct, id: string, props: GatewayServiceProps) {
    super(scope, id);
    const { vpc, config } = props;
    const stack = cdk.Stack.of(this);
    const arm = config.cpuArchitecture === 'arm64';

    // --- master key ------------------------------------------------------------
    // LiteLLM requires the key to start with "sk-". Secrets Manager generates the
    // random suffix; the container composes the full key at start so the
    // template never carries any part of it.
    this.masterKeySecret = new secretsmanager.Secret(this, 'MasterKey', {
      description: 'LiteLLM master key suffix; the full key is sk-<this value>',
      generateSecretString: { excludePunctuation: true, passwordLength: 48 },
    });

    // --- logs -------------------------------------------------------------------
    const logGroup = (name: string, removalPolicy = cdk.RemovalPolicy.DESTROY) =>
      new logs.LogGroup(this, `${name}Logs`, {
        logGroupName: `/${cdk.Aws.STACK_NAME}/${name.toLowerCase()}`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy,
      });
    const litellmLogs = logGroup('LiteLLM');
    // Keep the historical group's construct ID during the migration so an
    // existing deployment's logs are retained instead of deleted by the update.
    if (config.retainLegacyLogs) logGroup('Switchyard', cdk.RemovalPolicy.RETAIN);

    // --- task definition --------------------------------------------------------
    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'Task', {
      cpu: 1024,
      memoryLimitMiB: 2048,
      taskRole: props.taskRole,
      runtimePlatform: {
        cpuArchitecture: arm ? ecs.CpuArchitecture.ARM64 : ecs.CpuArchitecture.X86_64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const gatewayImage = new ecrAssets.DockerImageAsset(this, 'GatewayImage', {
      directory: path.resolve(__dirname, '..', 'docker', 'gateway'),
      platform: arm ? ecrAssets.Platform.LINUX_ARM64 : ecrAssets.Platform.LINUX_AMD64,
    });

    this.taskDefinition.addContainer('litellm', {
      containerName: 'litellm',
      image: ecs.ContainerImage.fromDockerImageAsset(gatewayImage),
      essential: true,
      portMappings: [{ containerPort: LITELLM_PORT, protocol: ecs.Protocol.TCP }],
      environment: {
        LITELLM_CONFIG_TEXT: props.litellmConfigText,
        SWITCHYARD_CONFIG_TEXT: props.switchyardConfigText,
        AWS_REGION: stack.region,
        LITELLM_LOG: 'INFO',
        // Use the cost map bundled in the pinned image so routing and model limits
        // don't change with a remote file.
        LITELLM_LOCAL_MODEL_COST_MAP: 'True',
        ...LITELLM_SURFACE_FLAGS,
      },
      secrets: {
        LITELLM_MASTER_KEY_SUFFIX: ecs.Secret.fromSecretsManager(this.masterKeySecret),
      },
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'litellm', logGroup: litellmLogs }),
      healthCheck: {
        command: [
          'CMD-SHELL',
          `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${LITELLM_PORT}/health/liveliness')"`,
        ],
        interval: cdk.Duration.seconds(10),
        timeout: cdk.Duration.seconds(5),
        retries: 5,
        startPeriod: cdk.Duration.seconds(90),
      },
    });

    // --- listener: HTTPS unless plain HTTP is explicitly allowed ----------------
    let tls: Pick<
      ecsPatterns.ApplicationLoadBalancedFargateServiceProps,
      'certificate' | 'domainName' | 'domainZone' | 'redirectHTTP' | 'protocol'
    > = {};
    if (config.domainName && config.hostedZone) {
      const zone = route53.HostedZone.fromLookup(this, 'Zone', { domainName: config.hostedZone });
      tls = {
        certificate: new acm.Certificate(this, 'Certificate', {
          domainName: config.domainName,
          validation: acm.CertificateValidation.fromDns(zone),
        }),
        domainName: config.domainName,
        domainZone: zone,
        redirectHTTP: true,
        protocol: elbv2.ApplicationProtocol.HTTPS,
      };
    } else if (config.domainName && config.certificateArn) {
      // An existing ACM certificate, for example for a domain whose DNS isn't in
      // Route 53. The stack creates no certificate and no DNS record; the
      // LoadBalancerDnsName output names the target for the reader's record.
      const certificateRegion = config.certificateArn.split(':')[3];
      if (!cdk.Token.isUnresolved(stack.region) && certificateRegion !== stack.region) {
        throw new ConfigValidationError(
          `certificateArn is in ${certificateRegion}, but the stack deploys to ${stack.region}. ` +
            'The load balancer needs an ACM certificate in its own Region.',
        );
      }
      tls = {
        certificate: acm.Certificate.fromCertificateArn(this, 'Certificate', config.certificateArn),
        redirectHTTP: true,
        protocol: elbv2.ApplicationProtocol.HTTPS,
      };
    } else if (config.allowPlainHttp) {
      // Plain HTTP only by explicit opt-in (docs/DECISIONS.md). Say so on every
      // synth and deploy.
      cdk.Annotations.of(this).addWarningV2(
        'switchyard-gateway:plain-http-listener',
        'The gateway listener is plain HTTP because allowPlainHttp=true is set. ' +
          'The master key (the Authorization header), prompts, tool results and source code, ' +
          'and model responses cross the network unencrypted. Use HTTP only for a short test ' +
          'with non-sensitive code from your own /32, then rotate the key, and run cdk destroy ' +
          'when you finish. To serve HTTPS, remove allowPlainHttp and pass -c domainName=<host> ' +
          'with -c hostedZone=<zone> or -c certificateArn=<arn>.',
      );
    } else {
      throw new ConfigValidationError(`The gateway has no HTTPS settings. ${LISTENER_HINT}`);
    }

    // --- service + load balancer ---------------------------------------------
    this.cluster = new ecs.Cluster(this, 'Cluster', {
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    this.service = new ecsPatterns.ApplicationLoadBalancedFargateService(this, 'Service', {
      cluster: this.cluster,
      taskDefinition: this.taskDefinition,
      desiredCount: config.desiredCount,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      taskSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      publicLoadBalancer: true,
      // No 0.0.0.0/0 ingress: the allowlist below is the only way in.
      openListener: false,
      idleTimeout: IDLE_TIMEOUT,
      circuitBreaker: { enable: true, rollback: true },
      healthCheckGracePeriod: cdk.Duration.seconds(120),
      ...tls,
    });
    this.loadBalancer = this.service.loadBalancer;
    // Drop headers whose names are not letters, digits, and hyphens (for example,
    // names with an underscore) before they reach LiteLLM. The headers the
    // sample's clients send and the ones LiteLLM reads keys from all qualify.
    this.loadBalancer.setAttribute('routing.http.drop_invalid_header_fields.enabled', 'true');

    this.service.targetGroup.configureHealthCheck({
      path: '/health/liveliness',
      interval: cdk.Duration.seconds(15),
      healthyThresholdCount: 2,
      unhealthyThresholdCount: 3,
    });
    this.service.targetGroup.setAttribute('deregistration_delay.timeout_seconds', '30');

    // Forward only the OpenAI-compatible API and the health check. LiteLLM's
    // admin UI, docs, and management routes get 404 from the load balancer.
    this.service.listener.addAction('ApiPaths', {
      priority: 10,
      conditions: [elbv2.ListenerCondition.pathPatterns(ALLOWED_PATHS)],
      action: elbv2.ListenerAction.forward([this.service.targetGroup]),
    });
    // A raw override, not listener.addAction('Default', ...): the pattern has
    // already set a forward default action, and replacing it through the typed
    // API emits the listenerExistingDefaultActionReplaced warning.
    (this.service.listener.node.defaultChild as elbv2.CfnListener).addPropertyOverride(
      'DefaultActions',
      [
        {
          Type: 'fixed-response',
          FixedResponseConfig: {
            StatusCode: '404',
            ContentType: 'text/plain',
            MessageBody: 'Not found',
          },
        },
      ],
    );

    // With a certificate: HTTPS on 443, plus 80 for the redirect to HTTPS. Without
    // one (allowPlainHttp=true only) the pattern's listener is plain HTTP on port
    // 80: the master key and every prompt and response cross the network
    // unencrypted between the client and the load balancer. The synth warning
    // above and the GatewayUrl output description say so.
    const listenerPort = tls.certificate ? 443 : 80;
    const allowed = ec2.Peer.ipv4(config.allowedCidr);
    this.loadBalancer.connections.allowFrom(
      allowed,
      ec2.Port.tcp(listenerPort),
      `Gateway clients (${config.allowedCidr})`,
    );
    if (tls.certificate) {
      this.loadBalancer.connections.allowFrom(allowed, ec2.Port.tcp(80), 'HTTP to HTTPS redirect');
    }

    this.gatewayUrl = tls.certificate
      ? `https://${config.domainName}`
      : `http://${this.loadBalancer.loadBalancerDnsName}`;

    // --- outputs ----------------------------------------------------------------
    // Logical IDs are pinned so scripts can query them by exact OutputKey.
    const output = (id: string, value: string, description: string) => {
      const out = new cdk.CfnOutput(this, id, { value, description });
      out.overrideLogicalId(id);
      return out;
    };
    output('RoutingProfile', config.routingProfile ?? 'stage', 'Active routing profile');
    output(
      'GatewayUrl',
      this.gatewayUrl,
      tls.certificate
        ? 'Base URL of the gateway. OpenAI-compatible API under /v1'
        : 'Base URL of the gateway over plain HTTP, for short tests only: the master key, ' +
            'prompts, and responses cross the network unencrypted. OpenAI-compatible API under /v1',
    );
    if (config.certificateArn) {
      output(
        'LoadBalancerDnsName',
        this.loadBalancer.loadBalancerDnsName,
        `Point ${config.domainName} at this name with a CNAME record (or an alias record in Route 53)`,
      );
    }
    output(
      'MasterKeySecretArn',
      this.masterKeySecret.secretArn,
      'Secrets Manager secret holding the master key suffix (full key is sk-<value>)',
    );
    // Key rotation forces a new deployment of this service (see the README).
    output('ClusterName', this.cluster.clusterName, 'ECS cluster that runs the gateway');
    output('ServiceName', this.service.service.serviceName, 'ECS service that runs the gateway');
    output(
      'MasterKeyCommand',
      `echo "sk-$(aws secretsmanager get-secret-value --region ${stack.region} ` +
        `--secret-id ${this.masterKeySecret.secretArn} --query SecretString --output text)"`,
      'Prints the LiteLLM master key',
    );
    output(
      'SmokeTestCommand',
      `STACK_NAME=${cdk.Aws.STACK_NAME} AWS_REGION=${stack.region} ROUTING_PROFILE=${config.routingProfile ?? 'stage'} scripts/smoke-test.sh`,
      'Runs the end-to-end smoke test against this deployment',
    );
  }
}
