import {
  ConfigValidationError,
  loadGatewayConfig,
  validateAllowedCidr,
  type ContextReader,
} from '../../lib/config';

function ctx(values: Record<string, unknown>): ContextReader {
  return { tryGetContext: (key: string) => values[key] };
}

describe('validateAllowedCidr', () => {
  test('accepts and normalizes a host and a network range', () => {
    expect(validateAllowedCidr('203.0.113.7/32')).toBe('203.0.113.7/32');
    expect(validateAllowedCidr(' 10.0.0.0/8 ')).toBe('10.0.0.0/8');
    expect(validateAllowedCidr('010.000.000.000/8')).toBe('10.0.0.0/8');
  });

  test.each([undefined, null, '', '   '])('requires a value (%p)', (value) => {
    expect(() => validateAllowedCidr(value)).toThrow(/allowedCidr is required/);
  });

  test.each(['0.0.0.0/0', '0.0.0.0/32', '0.0.0.0/8', '10.0.0.0/0', '203.0.113.7/0'])(
    'rejects the world-open range %s',
    (value) => {
      expect(() => validateAllowedCidr(value)).toThrow(/whole internet/);
    },
  );

  test('rejects IPv6 and ::/0', () => {
    expect(() => validateAllowedCidr('::/0')).toThrow(/IPv4/);
    expect(() => validateAllowedCidr('2001:db8::/32')).toThrow(/IPv4/);
  });

  test.each(['203.0.113.7', '203.0.113.7/33', '256.0.0.1/32', 'abc/24', '1.2.3/24'])(
    'rejects the malformed value %s',
    (value) => {
      expect(() => validateAllowedCidr(value)).toThrow(ConfigValidationError);
    },
  );
});

const CERTIFICATE_ARN =
  'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555';

describe('loadGatewayConfig', () => {
  test.each([true, 'true'])('applies defaults with allowPlainHttp=%p', (allowPlainHttp) => {
    expect(loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', allowPlainHttp }))).toEqual({
      allowedCidr: '203.0.113.0/24',
      routingProfile: 'stage',
      desiredCount: 1,
      cpuArchitecture: 'arm64',
      allowPlainHttp: true,
    });
  });

  test.each([undefined, false, 'false'])(
    'requires HTTPS settings or the plain-HTTP opt-in (allowPlainHttp=%p)',
    (allowPlainHttp) => {
      expect(() =>
        loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', allowPlainHttp })),
      ).toThrow(/domainName[\s\S]*hostedZone[\s\S]*certificateArn[\s\S]*-c allowPlainHttp=true/);
    },
  );

  test.each(['yes', 1])('rejects allowPlainHttp=%p', (allowPlainHttp) => {
    expect(() => loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', allowPlainHttp }))).toThrow(
      /allowPlainHttp must be true or false/,
    );
  });

  test.each([
    { domainName: 'llm-gateway.example.com', hostedZone: 'example.com' },
    { domainName: 'llm-gateway.example.com', certificateArn: CERTIFICATE_ARN },
    { hostedZone: 'example.com' },
    { certificateArn: CERTIFICATE_ARN },
  ])('rejects allowPlainHttp=true with an HTTPS setting (%p)', (https) => {
    expect(() =>
      loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', allowPlainHttp: 'true', ...https })),
    ).toThrow(/cannot be combined/);
  });

  test('accepts allowPlainHttp=false beside HTTPS settings', () => {
    expect(
      loadGatewayConfig(
        ctx({
          allowedCidr: '203.0.113.0/24',
          domainName: 'llm-gateway.example.com',
          hostedZone: 'example.com',
          allowPlainHttp: 'false',
        }),
      ),
    ).not.toHaveProperty('allowPlainHttp');
  });

  test('reads an existing certificate for the domain', () => {
    expect(
      loadGatewayConfig(
        ctx({
          allowedCidr: '203.0.113.0/24',
          domainName: 'llm-gateway.example.com',
          certificateArn: CERTIFICATE_ARN,
        }),
      ),
    ).toEqual({
      allowedCidr: '203.0.113.0/24',
      routingProfile: 'stage',
      domainName: 'llm-gateway.example.com',
      certificateArn: CERTIFICATE_ARN,
      desiredCount: 1,
      cpuArchitecture: 'arm64',
    });
  });

  test('rejects certificateArn without domainName or with hostedZone', () => {
    expect(() =>
      loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', certificateArn: CERTIFICATE_ARN })),
    ).toThrow(/certificateArn needs domainName/);
    expect(() =>
      loadGatewayConfig(
        ctx({
          allowedCidr: '203.0.113.0/24',
          domainName: 'llm-gateway.example.com',
          hostedZone: 'example.com',
          certificateArn: CERTIFICATE_ARN,
        }),
      ),
    ).toThrow(/not both/);
  });

  test.each([
    'arn:aws:iam::123456789012:server-certificate/gateway',
    'arn:aws:acm:us-east-1:12345:certificate/abc',
    'arn:aws:acm:us-east-1:123456789012:certificate/',
    'not-an-arn',
  ])('rejects the certificate ARN %s', (certificateArn) => {
    expect(() =>
      loadGatewayConfig(
        ctx({
          allowedCidr: '203.0.113.0/24',
          domainName: 'llm-gateway.example.com',
          certificateArn,
        }),
      ),
    ).toThrow(/is not an ACM certificate ARN/);
  });

  test('fails without allowedCidr and names the flag to pass', () => {
    expect(() => loadGatewayConfig(ctx({}))).toThrow(/-c allowedCidr=/);
  });

  test('reads every option', () => {
    expect(
      loadGatewayConfig(
        ctx({
          allowedCidr: '203.0.113.0/24',
          domainName: 'llm-gateway.example.com',
          hostedZone: 'example.com',
          desiredCount: '2',
          cpuArchitecture: 'x86_64',
        }),
      ),
    ).toEqual({
      allowedCidr: '203.0.113.0/24',
      routingProfile: 'stage',
      domainName: 'llm-gateway.example.com',
      hostedZone: 'example.com',
      desiredCount: 2,
      cpuArchitecture: 'x86_64',
    });
  });

  test('requires domainName and hostedZone together', () => {
    expect(() =>
      loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', domainName: 'a.example.com' })),
    ).toThrow(/set together/);
    expect(() =>
      loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', hostedZone: 'example.com' })),
    ).toThrow(/set together/);
  });

  test('requires domainName to be inside hostedZone', () => {
    expect(() =>
      loadGatewayConfig(
        ctx({
          allowedCidr: '203.0.113.0/24',
          domainName: 'a.other.com',
          hostedZone: 'example.com',
        }),
      ),
    ).toThrow(/inside hostedZone/);
    expect(() =>
      loadGatewayConfig(
        ctx({ allowedCidr: '203.0.113.0/24', domainName: 'not a host', hostedZone: 'example.com' }),
      ),
    ).toThrow(/valid DNS names/);
  });

  test.each([0, -1, 1.5, 'two', '0'])('rejects desiredCount %p', (value) => {
    expect(() =>
      loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', desiredCount: value })),
    ).toThrow(/desiredCount must be an integer >= 1/);
  });

  test('rejects an unknown cpuArchitecture', () => {
    expect(() =>
      loadGatewayConfig(ctx({ allowedCidr: '203.0.113.0/24', cpuArchitecture: 'amd64' })),
    ).toThrow(/cpuArchitecture must be "arm64" or "x86_64"/);
  });
});
