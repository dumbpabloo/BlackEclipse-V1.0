import { describe, expect, it } from 'vitest';
import { buildAgentInstruction } from './command-agent';
import { resolveCommand } from './command-registry';

describe('/deck command', () => {
  it('is an agent-turn command with presentation aliases', () => {
    const spec = resolveCommand('deck');
    expect(spec?.handler.type).toBe('agent');
    expect(spec?.handler.target).toBe('agent.deck');
    expect(resolveCommand('slides')?.name).toBe('deck');
    expect(resolveCommand('presentation')?.name).toBe('deck');
  });

  it('rewrites the request into a deck-building directive that forbids invented data', () => {
    const result = buildAgentInstruction('/deck a Q3 update for AAPL --theme paper --slides 8');
    expect(result?.command).toBe('deck');
    expect(result?.instruction).toContain('presentation_create');
    expect(result?.instruction).toContain('a Q3 update for AAPL');
    expect(result?.instruction).toContain('Theme: paper');
    expect(result?.instruction).toContain('Target slide count: 8');
    expect(result?.instruction).toContain('Never invent a number');
    expect(result?.instruction).toContain('UNAVAILABLE');
  });

  it('asks for the subject instead of inventing one', () => {
    const result = buildAgentInstruction('/deck');
    expect(result?.instruction).toContain('ask the user what the deck is about and STOP');
  });
});
