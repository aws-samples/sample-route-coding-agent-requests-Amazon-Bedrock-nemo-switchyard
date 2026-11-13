import * as fs from 'node:fs';
import * as path from 'node:path';

interface ChatMessage {
  role: string;
  content?: string | null;
  tool_call_id?: string;
  tool_calls?: { id: string; function: { name: string; arguments: string } }[];
}

interface Fixture {
  model: string;
  messages: ChatMessage[];
}

const FIXTURE = path.resolve(__dirname, '..', '..', 'scripts', 'fixtures', 'stage-escalation.json');

describe('scripts/fixtures/stage-escalation.json', () => {
  const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8')) as Fixture;

  test('targets the public model group and is a well-formed OpenAI tool conversation', () => {
    expect(fixture.model).toBe('switchyard');
    const callIds = new Set<string>();
    for (const m of fixture.messages) {
      if (m.tool_calls) {
        for (const c of m.tool_calls) {
          callIds.add(c.id);
          expect(() => JSON.parse(c.function.arguments) as unknown).not.toThrow();
        }
      }
      if (m.role === 'tool') expect(callIds).toContain(m.tool_call_id);
    }
  });

  test('ends with the same hard failure twice inside the stage router recent window', () => {
    // The stage router's `repeated_failure` override needs an identical failure
    // fingerprint at least twice in the last three tool results.
    const toolResults = fixture.messages.filter((m) => m.role === 'tool').slice(-3);
    const failures = toolResults.filter((m) => /AssertionError/.test(m.content ?? ''));
    expect(failures.length).toBeGreaterThanOrEqual(2);
    expect(new Set(failures.map((m) => m.content)).size).toBe(1);
  });

  test('uses tool names from the built-in coding vocabulary (bash, edit)', () => {
    const names = new Set(
      fixture.messages.flatMap((m) => m.tool_calls?.map((c) => c.function.name) ?? []),
    );
    expect(names).toEqual(new Set(['bash', 'edit']));
  });
});
