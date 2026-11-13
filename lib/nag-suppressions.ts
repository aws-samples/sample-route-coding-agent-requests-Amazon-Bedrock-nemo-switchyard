import * as cdk from 'aws-cdk-lib';
import type { IConstruct } from 'constructs';
import { bedrockInvokeResources } from './bedrock-policy';
import type { GatewayStack } from './gateway-stack';

/**
 * cdk-nag (AwsSolutions pack) findings this sample accepts, each with the
 * reason. Review them before reusing the stack in production; most point at
 * features worth enabling there (VPC flow logs, ALB access logs, key rotation).
 */
export function acknowledgeKnownFindings(stack: GatewayStack): void {
  const ack = (scope: IConstruct, id: string, reason: string) =>
    cdk.Validations.of(scope).acknowledge({ id, reason });

  ack(
    stack.network.vpc,
    'AwsSolutions-VPC7',
    'Sample deployment: VPC flow logs are left off to limit CloudWatch Logs cost. ' +
      'Enable them for production with vpc.addFlowLog().',
  );
  ack(
    stack.network.bedrockEndpoint,
    'AwsSolutions-EC23',
    'The endpoint security group allows 443 from the VPC CIDR only; cdk-nag cannot ' +
      'evaluate the intrinsic Fn::GetAtt reference to that CIDR.',
  );
  ack(
    stack.gateway.loadBalancer,
    'AwsSolutions-ELB2',
    'Sample deployment: ALB access logs need an S3 bucket and are left off to limit ' +
      'resources and cost. Enable them for production with loadBalancer.logAccessLogs().',
  );
  ack(
    stack.gateway.masterKeySecret,
    'AwsSolutions-SMG4',
    'The LiteLLM master key has no rotation Lambda in this sample; LiteLLM reads it only ' +
      'at start. Rotate by updating the secret and redeploying the service.',
  );
  ack(
    stack.gateway.taskDefinition,
    'AwsSolutions-ECS2',
    'The plain environment carries only non-secret values: LITELLM_CONFIG_TEXT and ' +
      'SWITCHYARD_CONFIG_TEXT (the litellm.yaml and switchyard.toml profile files), AWS_REGION, ' +
      'LITELLM_LOG, LITELLM_LOCAL_MODEL_COST_MAP, and the NO_DOCS, NO_REDOC, NO_OPENAPI, and ' +
      'DISABLE_ADMIN_UI switches. The ' +
      'master key is injected from Secrets Manager through the Secrets field.',
  );
  ack(
    stack.gateway.taskDefinition,
    'AwsSolutions-IAM5[Resource::*]',
    'ecr:GetAuthorizationToken, which the execution role needs to pull the gateway image ' +
      'from Amazon ECR, only supports the * resource.',
  );

  // Build each ID from the values cdk-nag sees in the template. With a concrete
  // env, cdk.json's @aws-cdk/core:enablePartitionLiterals renders the partition
  // as a literal (aws); without it the partition stays the AWS::Partition token.
  const region = cdk.Token.isUnresolved(stack.region) ? '<AWS::Region>' : stack.region;
  const account = cdk.Token.isUnresolved(stack.account) ? '<AWS::Account>' : stack.account;
  const partition = cdk.Token.isUnresolved(stack.partition) ? '<AWS::Partition>' : stack.partition;
  const wildcardArns = bedrockInvokeResources(
    stack.litellm.models.map((m) => m.model),
    region,
    account,
    partition,
  ).filter((arn) => arn.includes('*'));
  for (const arn of wildcardArns) {
    ack(
      stack.taskRole,
      `AwsSolutions-IAM5[Resource::${arn}]`,
      'A cross-Region inference profile can route to its base foundation model in other ' +
        'Regions, so the Region is *. The trailing * covers version suffixes such as -v1:0, ' +
        'but it matches every model ID that starts with this prefix. The grant is not ' +
        'conditioned on the inference profile ARN, so the role can also invoke the base model ' +
        'directly. Only InvokeModel and InvokeModelWithResponseStream are granted.',
    );
  }
}
