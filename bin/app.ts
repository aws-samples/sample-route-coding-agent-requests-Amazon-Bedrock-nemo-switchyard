#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { ConfigValidationError, loadGatewayConfig } from '../lib/config';
import { GatewayStack } from '../lib/gateway-stack';

const app = new cdk.App();

let config;
try {
  config = loadGatewayConfig(app.node);
} catch (error) {
  if (error instanceof ConfigValidationError) {
    console.error(`\nConfiguration error: ${error.message}\n`);
    process.exit(1);
  }
  throw error;
}

const stack = new GatewayStack(app, 'SwitchyardGateway', {
  config,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description:
    'LiteLLM gateway with the NVIDIA Switchyard routing plugin for Amazon Bedrock (sample code)',
});

cdk.Tags.of(stack).add('Project', 'switchyard-bedrock-gateway');
