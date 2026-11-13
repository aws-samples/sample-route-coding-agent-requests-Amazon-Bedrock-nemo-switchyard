import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { BEDROCK_INVOKE_ACTIONS, bedrockInvokeResources } from './bedrock-policy';
import type { GatewayConfig } from './config';
import { GatewayService } from './gateway-service';
import { acknowledgeKnownFindings } from './nag-suppressions';
import { GatewayNetwork } from './network';
import { GatewayWaf } from './waf';
import {
  loadLiteLLMConfig,
  profileDirectory,
  loadSwitchyardConfig,
  validateRoutingConfig,
  type LiteLLMConfig,
  type SwitchyardConfig,
} from './routing-config';

export interface GatewayStackProps extends cdk.StackProps {
  readonly config: GatewayConfig;
  /** Override the configuration files (tests). Defaults to the selected profile. */
  readonly litellmConfigPath?: string;
  readonly switchyardConfigPath?: string;
}

/**
 * LiteLLM gateway with the NVIDIA Switchyard routing plugin for Amazon Bedrock.
 *
 * One Fargate task runs the LiteLLM proxy with the Switchyard routing plugin.
 */
export class GatewayStack extends cdk.Stack {
  public readonly config: GatewayConfig;
  public readonly litellm: LiteLLMConfig;
  public readonly switchyard: SwitchyardConfig;
  public readonly network: GatewayNetwork;
  public readonly taskRole: iam.Role;
  public readonly gateway: GatewayService;
  public readonly waf: GatewayWaf;

  constructor(scope: Construct, id: string, props: GatewayStackProps) {
    super(scope, id, props);
    this.config = props.config;

    // Fail at synth time if the two configuration files disagree.
    const directory = profileDirectory(props.config.routingProfile);
    this.litellm = loadLiteLLMConfig(
      props.litellmConfigPath ?? path.join(directory, 'litellm.yaml'),
    );
    this.switchyard = loadSwitchyardConfig(
      props.switchyardConfigPath ?? path.join(directory, 'switchyard.toml'),
    );
    const problems = validateRoutingConfig(this.litellm, this.switchyard);
    if (problems.length > 0) {
      throw new Error(`Routing configuration is inconsistent:\n  - ${problems.join('\n  - ')}`);
    }

    this.network = new GatewayNetwork(this, 'Network');

    // Derive Bedrock permissions from the active profile’s model inventory.
    this.taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'LiteLLM gateway task role: invoke the configured Amazon Bedrock models',
    });
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeConfiguredBedrockModels',
        actions: BEDROCK_INVOKE_ACTIONS,
        resources: bedrockInvokeResources(
          this.litellm.models.map((m) => m.model),
          this.region,
          this.account,
          this.partition,
        ),
      }),
    );

    this.gateway = new GatewayService(this, 'Gateway', {
      vpc: this.network.vpc,
      config: this.config,
      taskRole: this.taskRole,
      switchyardConfigText: this.switchyard.text,
      litellmConfigText: this.litellm.text,
    });

    this.waf = new GatewayWaf(this, 'Waf', { loadBalancer: this.gateway.loadBalancer });

    acknowledgeKnownFindings(this);
  }
}
