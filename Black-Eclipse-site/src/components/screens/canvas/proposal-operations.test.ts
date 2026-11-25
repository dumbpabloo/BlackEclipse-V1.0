import { describe, expect, it } from 'vitest';
import type { CanvasProposal } from '@/lib/harness/clients/canvas-client';
import { proposalOperationCount, proposalOperations } from './canvas-utils';

function proposal(overrides: Partial<CanvasProposal>): CanvasProposal {
  return {
    artifactId: 'art_1',
    baseVersion: 1,
    currentVersion: 1,
    mode: 'edit',
    answer: '',
    summary: '',
    operations: [],
    notes: [],
    ...overrides,
  };
}

describe('proposal operations', () => {
  it('uses deck ops for a presentation proposal', () => {
    const value = proposal({
      deckOperations: [{ op: 'set_title', slideId: 's1', text: 'AI title' }],
    });
    expect(proposalOperations(value)).toEqual([{ op: 'set_title', slideId: 's1', text: 'AI title' }]);
    expect(proposalOperationCount(value)).toBe(1);
  });

  it('falls back to text ops and handles an empty proposal', () => {
    const value = proposal({
      operations: [{ op: 'append', content: 'x' }],
    });
    expect(proposalOperations(value)).toHaveLength(1);
    expect(proposalOperations(proposal({}))).toEqual([]);
    expect(proposalOperationCount(null)).toBe(0);
  });
});
