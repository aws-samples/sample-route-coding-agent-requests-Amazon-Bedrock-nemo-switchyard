/**
 * Deployment configuration, read from CDK context (`-c key=value` or cdk.json).
 *
 * Validation fails closed: a missing or world-open `allowedCidr` stops synth
 * before any template is produced, and so does a listener with no HTTPS
 * settings unless `allowPlainHttp=true` opts in to plain HTTP.
 */

export type CpuArchitecture = 'arm64' | 'x86_64';

export interface GatewayConfig {
  /** Directory under profiles/, selected at deployment. Defaults to stage. */
  readonly routingProfile?: string;
  /** Migration only: retain the previous standalone-server log group. */
  readonly retainLegacyLogs?: boolean;
  /** IPv4 CIDR allowed to reach the load balancer. Required; never 0.0.0.0/0. */
  readonly allowedCidr: string;
  /** Serve HTTPS on this name, with `hostedZone` or `certificateArn`. */
  readonly domainName?: string;
  /** Route 53 public hosted zone that contains `domainName`; CDK creates the certificate and alias. */
  readonly hostedZone?: string;
  /** Existing ACM certificate for `domainName` in the stack's Region; CDK creates no DNS record. */
  readonly certificateArn?: string;
  /** Explicit opt-in to a plain-HTTP listener for a short test. Never combined with HTTPS settings. */
  readonly allowPlainHttp?: boolean;
  /** Number of Fargate tasks. */
  readonly desiredCount: number;
  /** CPU architecture for the Fargate tasks and the container images. */
  readonly cpuArchitecture: CpuArchitecture;
}

export class ConfigValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigValidationError';
  }
}

/** Minimal reader interface so the validator can be unit tested without a CDK app. */
export interface ContextReader {
  tryGetContext(key: string): unknown;
}

const CIDR_PATTERN = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/;

/**
 * Validate an IPv4 CIDR for use as an ingress allowlist. Rejects anything that
 * would open the gateway to the internet (prefix length 0 or an all-zeros
 * network, in any spelling) and IPv6 `::/0`.
 */
export function validateAllowedCidr(value: unknown): string {
  const hint =
    'Pass the network that may reach the gateway, for example: ' +
    '-c allowedCidr="$(curl -s https://checkip.amazonaws.com)/32"';
  if (typeof value !== 'string' || value.trim() === '') {
    throw new ConfigValidationError(`allowedCidr is required. ${hint}`);
  }
  const cidr = value.trim();
  if (cidr === '::/0' || cidr.includes(':')) {
    throw new ConfigValidationError(
      `allowedCidr "${cidr}" is not supported: use an IPv4 CIDR, and never a world-open range. ${hint}`,
    );
  }
  const match = CIDR_PATTERN.exec(cidr);
  if (!match) {
    throw new ConfigValidationError(
      `allowedCidr "${cidr}" is not a valid IPv4 CIDR (a.b.c.d/n). ${hint}`,
    );
  }
  const octets = match.slice(1, 5).map(Number);
  const prefix = Number(match[5]);
  if (octets.some((o) => o > 255) || prefix > 32) {
    throw new ConfigValidationError(
      `allowedCidr "${cidr}" is not a valid IPv4 CIDR (a.b.c.d/n). ${hint}`,
    );
  }
  if (prefix === 0 || octets.every((o) => o === 0)) {
    throw new ConfigValidationError(
      `allowedCidr "${cidr}" would expose a billed Amazon Bedrock entry point to the whole internet. ` +
        `This sample refuses 0.0.0.0/0 and /0 ranges. ${hint}`,
    );
  }
  return `${octets.join('.')}/${prefix}`;
}

function readOptionalString(ctx: ContextReader, key: string): string | undefined {
  const value = ctx.tryGetContext(key);
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'string') {
    throw new ConfigValidationError(`${key} must be a string, got ${JSON.stringify(value)}`);
  }
  return value.trim();
}

function readDesiredCount(ctx: ContextReader): number {
  const value = ctx.tryGetContext('desiredCount');
  if (value === undefined || value === null || value === '') return 1;
  const n =
    typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isInteger(n) || n < 1) {
    throw new ConfigValidationError(
      `desiredCount must be an integer >= 1, got ${JSON.stringify(value)}. Example: -c desiredCount=2`,
    );
  }
  return n;
}

function readCpuArchitecture(ctx: ContextReader): CpuArchitecture {
  const value = ctx.tryGetContext('cpuArchitecture');
  if (value === undefined || value === null || value === '') return 'arm64';
  if (value === 'arm64' || value === 'x86_64') return value;
  throw new ConfigValidationError(
    `cpuArchitecture must be "arm64" or "x86_64", got ${JSON.stringify(value)}. Example: -c cpuArchitecture=x86_64`,
  );
}

const HOSTNAME_PATTERN = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i;
const CERTIFICATE_ARN_PATTERN = /^arn:aws[a-z-]*:acm:[a-z0-9-]+:\d{12}:certificate\/[A-Za-z0-9-]+$/;

/** How to serve HTTPS, or opt in to plain HTTP, named in every listener error. */
export const LISTENER_HINT =
  'Serve HTTPS with -c domainName=<host> plus either -c hostedZone=<Route 53 public hosted zone> ' +
  'or -c certificateArn=<ACM certificate ARN in the stack Region>. For a short test from your own ' +
  'IP address only, opt in to plain HTTP with -c allowPlainHttp=true.';

function readAllowPlainHttp(ctx: ContextReader): boolean {
  const value = ctx.tryGetContext('allowPlainHttp');
  if (value === undefined || value === null || value === '') return false;
  if (value === true || value === 'true') return true;
  if (value === false || value === 'false') return false;
  throw new ConfigValidationError(
    `allowPlainHttp must be true or false, got ${JSON.stringify(value)}`,
  );
}

/** Build and validate the configuration from CDK context. */
export function loadGatewayConfig(ctx: ContextReader): GatewayConfig {
  const allowedCidr = validateAllowedCidr(ctx.tryGetContext('allowedCidr'));
  const domainName = readOptionalString(ctx, 'domainName');
  const hostedZone = readOptionalString(ctx, 'hostedZone');
  const certificateArn = readOptionalString(ctx, 'certificateArn');
  const allowPlainHttp = readAllowPlainHttp(ctx);

  // Fail closed: an opt-in to plain HTTP never sits beside an HTTPS setting.
  if (allowPlainHttp && (domainName ?? hostedZone ?? certificateArn) !== undefined) {
    throw new ConfigValidationError(
      'allowPlainHttp=true cannot be combined with domainName, hostedZone, or certificateArn. ' +
        'Remove allowPlainHttp to serve HTTPS, or remove the HTTPS settings for a plain-HTTP test.',
    );
  }
  if (certificateArn !== undefined) {
    if (domainName === undefined) {
      throw new ConfigValidationError(
        'certificateArn needs domainName, the name the certificate covers, for example: ' +
          '-c domainName=llm-gateway.example.com -c certificateArn=arn:aws:acm:...',
      );
    }
    if (hostedZone !== undefined) {
      throw new ConfigValidationError(
        'Set hostedZone (CDK creates the certificate and the DNS record) or certificateArn ' +
          '(you provide the certificate and the DNS record), not both',
      );
    }
    if (!CERTIFICATE_ARN_PATTERN.test(certificateArn)) {
      throw new ConfigValidationError(
        `certificateArn "${certificateArn}" is not an ACM certificate ARN ` +
          '(arn:aws:acm:<region>:<account>:certificate/<id>)',
      );
    }
    if (!HOSTNAME_PATTERN.test(domainName)) {
      throw new ConfigValidationError(`domainName "${domainName}" must be a valid DNS name`);
    }
  } else if ((domainName === undefined) !== (hostedZone === undefined)) {
    throw new ConfigValidationError(
      'domainName and hostedZone must be set together, or domainName with certificateArn, ' +
        'for example: -c domainName=llm-gateway.example.com -c hostedZone=example.com',
    );
  }
  if (domainName !== undefined && hostedZone !== undefined) {
    if (!HOSTNAME_PATTERN.test(domainName) || !HOSTNAME_PATTERN.test(hostedZone)) {
      throw new ConfigValidationError(
        `domainName "${domainName}" and hostedZone "${hostedZone}" must be valid DNS names`,
      );
    }
    const dn = domainName.toLowerCase();
    const hz = hostedZone.toLowerCase();
    if (dn !== hz && !dn.endsWith(`.${hz}`)) {
      throw new ConfigValidationError(
        `domainName "${domainName}" must be inside hostedZone "${hostedZone}"`,
      );
    }
  }

  const routingProfile = readOptionalString(ctx, 'routingProfile') ?? 'stage';
  if (!/^[a-z][a-z0-9-]*$/.test(routingProfile)) {
    throw new ConfigValidationError('routingProfile must name a directory under profiles/');
  }
  const retainLegacyLogs = ctx.tryGetContext('retainLegacyLogs');
  if (
    retainLegacyLogs !== undefined &&
    retainLegacyLogs !== true &&
    retainLegacyLogs !== false &&
    retainLegacyLogs !== 'true' &&
    retainLegacyLogs !== 'false'
  ) {
    throw new ConfigValidationError('retainLegacyLogs must be true or false');
  }
  const config: GatewayConfig = {
    routingProfile,
    ...(retainLegacyLogs === true || retainLegacyLogs === 'true' ? { retainLegacyLogs: true } : {}),
    allowedCidr,
    desiredCount: readDesiredCount(ctx),
    cpuArchitecture: readCpuArchitecture(ctx),
    ...(domainName !== undefined ? { domainName } : {}),
    ...(hostedZone !== undefined ? { hostedZone } : {}),
    ...(certificateArn !== undefined ? { certificateArn } : {}),
    ...(allowPlainHttp ? { allowPlainHttp: true } : {}),
  };
  // HTTPS unless plain HTTP is explicitly allowed.
  if (domainName === undefined && !allowPlainHttp) {
    throw new ConfigValidationError(`No HTTPS settings were given. ${LISTENER_HINT}`);
  }
  return config;
}
