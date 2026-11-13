import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

/**
 * VPC for the gateway: two Availability Zones, public subnets for the load
 * balancer, private subnets with NAT egress for the Fargate tasks, and an
 * interface endpoint so Amazon Bedrock Runtime traffic stays on the AWS network.
 *
 * The NAT gateway remains for everything else the task needs: pulling the
 * gateway image (LiteLLM with the Switchyard plugin) from Amazon ECR, and
 * reaching Secrets Manager and CloudWatch Logs.
 */
export class GatewayNetwork extends Construct {
  public readonly vpc: ec2.Vpc;
  public readonly bedrockEndpoint: ec2.InterfaceVpcEndpoint;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1,
      restrictDefaultSecurityGroup: true,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
    });

    this.bedrockEndpoint = this.vpc.addInterfaceEndpoint('BedrockRuntime', {
      service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
      privateDnsEnabled: true,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });
  }
}
