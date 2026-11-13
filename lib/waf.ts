import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

export interface GatewayWafProps {
  readonly loadBalancer: elbv2.IApplicationLoadBalancer;
  /** Requests per 5 minutes per source IP before the rate rule blocks. */
  readonly rateLimitPer5Minutes?: number;
}

/**
 * AWS WAF web ACL for the gateway's load balancer.
 *
 * Agent requests carry source code and long prompts, which trip the generic
 * managed body rules (cross-site scripting, SQL injection, 8 KB body size).
 * This ACL uses the Amazon IP reputation list, the known-bad-inputs rule
 * group, and a per-IP rate limit instead, so normal agent traffic passes.
 */
export class GatewayWaf extends Construct {
  public readonly webAcl: wafv2.CfnWebACL;

  constructor(scope: Construct, id: string, props: GatewayWafProps) {
    super(scope, id);
    const rateLimit = props.rateLimitPer5Minutes ?? 2000;

    // Sampled requests keep every request header for three hours, including the
    // master key in Authorization, for anyone allowed wafv2:GetSampledRequests.
    // CloudWatch metrics stay on.
    const visibility = (metricName: string): wafv2.CfnWebACL.VisibilityConfigProperty => ({
      cloudWatchMetricsEnabled: true,
      metricName,
      sampledRequestsEnabled: false,
    });

    const managed = (name: string, priority: number): wafv2.CfnWebACL.RuleProperty => ({
      name,
      priority,
      statement: {
        managedRuleGroupStatement: { vendorName: 'AWS', name },
      },
      overrideAction: { none: {} },
      visibilityConfig: visibility(name),
    });

    this.webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      description: 'Switchyard LiteLLM gateway: reputation, known bad inputs, per-IP rate limit',
      visibilityConfig: visibility('SwitchyardGateway'),
      rules: [
        managed('AWSManagedRulesAmazonIpReputationList', 0),
        managed('AWSManagedRulesKnownBadInputsRuleSet', 1),
        {
          name: 'RateLimitPerIp',
          priority: 2,
          statement: {
            rateBasedStatement: { limit: rateLimit, aggregateKeyType: 'IP' },
          },
          action: { block: {} },
          visibilityConfig: visibility('RateLimitPerIp'),
        },
      ],
    });

    new wafv2.CfnWebACLAssociation(this, 'Association', {
      resourceArn: props.loadBalancer.loadBalancerArn,
      webAclArn: this.webAcl.attrArn,
    });
  }
}
