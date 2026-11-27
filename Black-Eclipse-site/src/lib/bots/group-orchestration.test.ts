import { describe, expect, it } from 'vitest';

import { buildContextPacket, replyOwner, routeGroupRequest } from './group-orchestration';

const MEMBERS = [
  { id: 'a', name: 'Ana', title: 'Macro analyst', description: 'Rates, FX and central banks' },
  { id: 'b', name: 'Ben', title: 'Equity analyst', description: 'Earnings and valuation' },
  { id: 'c', name: 'Cleo', title: 'Risk officer', description: 'Position sizing and drawdown' },
];

describe('routeGroupRequest', () => {
  it('routes a clear topic to the member who owns it', () => {
    expect(routeGroupRequest('What do rates and central banks imply?', MEMBERS, 'c')).toMatchObject({
      ownerId: 'a', reason: 'keyword',
    });
  });

  it('routes an unmatched request to the lead', () => {
    expect(routeGroupRequest('Thoughts?', MEMBERS, 'c')).toMatchObject({
      ownerId: 'c', reason: 'lead',
    });
  });

  it('broadcasts when there is no lead and no signal', () => {
    expect(routeGroupRequest('Thoughts?', MEMBERS, null)).toMatchObject({ reason: 'broadcast' });
  });

  it('does not let a single shared word beat a stronger match', () => {
    const decision = routeGroupRequest('equity valuation and drawdown risk', MEMBERS, 'a');
    // "equity"/"valuation" (Ben) and "risk"/"drawdown" (Cleo) tie; the lead decides.
    expect(decision.ownerId).toBe('a');
    expect(decision.reason).toBe('lead');
  });
});

describe('buildContextPacket', () => {
  it('includes the request, members and newest replies, bounded', () => {
    const packet = buildContextPacket({
      groupName: 'Markets Desk',
      members: ['Ana', 'Ben'],
      request: 'Should we add duration?',
      budgetChars: 1_000,
      prior: [
        { role: 'user', content: 'old request' },
        { role: 'assistant', content: 'old reply', authorName: 'Ana' },
        { role: 'assistant', content: 'new reply', authorName: 'Ben' },
      ],
    });
    expect(packet).toContain('Room: Markets Desk');
    expect(packet).toContain('Current request: Should we add duration?');
    expect(packet).toContain('Ben: new reply');
    expect(packet).toContain('Ana: old reply');
  });

  it('drops the oldest turns when over budget and says so', () => {
    const long = 'x'.repeat(900);
    const packet = buildContextPacket({
      groupName: 'Desk',
      members: ['Ana'],
      request: 'q',
      budgetChars: 1_000,
      prior: [
        { role: 'assistant', content: `old ${long}`, authorName: 'Ana' },
        { role: 'assistant', content: `new ${long}`, authorName: 'Ben' },
      ],
    });
    expect(packet).toContain('omitted for context budget');
    expect(packet).toContain('Ben:');
  });
});

describe('replyOwner', () => {
  it('resolves the author of the replied-to message', () => {
    const messages = [{ id: 'm1', authorId: 'a' }, { id: 'm2', authorId: 'b' }];
    expect(replyOwner(messages, 'm2')).toBe('b');
    expect(replyOwner(messages, 'missing')).toBeUndefined();
    expect(replyOwner(messages, null)).toBeUndefined();
  });
});
