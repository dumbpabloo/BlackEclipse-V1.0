import { beforeEach, describe, expect, it } from 'vitest';

import { useBotGroupsStore } from './groups-store';

describe('group room mode', () => {
  beforeEach(() => {
    useBotGroupsStore.setState({ groups: [], byGroup: {}, hydrated: true });
  });

  it('defaults a new room to independent takes', () => {
    const group = useBotGroupsStore.getState().createGroup({
      name: 'Research room',
      memberIds: ['a', 'b'],
    });
    expect(group.mode).toBe('parallel');
  });

  it('creates a roundtable room and changes mode later', () => {
    const group = useBotGroupsStore.getState().createGroup({
      name: 'Committee',
      memberIds: ['a', 'b', 'c'],
      mode: 'roundtable',
    });
    expect(group.mode).toBe('roundtable');

    useBotGroupsStore.getState().setGroupMode(group.id, 'parallel');
    expect(useBotGroupsStore.getState().groups[0].mode).toBe('parallel');

    useBotGroupsStore.getState().setGroupMode(group.id, 'roundtable');
    expect(useBotGroupsStore.getState().groups[0].mode).toBe('roundtable');
  });

  it('stores lead routing separately from the chair', () => {
    const group = useBotGroupsStore.getState().createGroup({
      name: 'Committee',
      memberIds: ['a', 'b'],
    });
    expect(group.routingMode).toBe('all');
    useBotGroupsStore.getState().setGroupLead(group.id, 'a');
    useBotGroupsStore.getState().setGroupRouting(group.id, 'lead');
    expect(useBotGroupsStore.getState().groups[0].routingMode).toBe('lead');
    expect(useBotGroupsStore.getState().groups[0].leadId).toBe('a');
    useBotGroupsStore.getState().setGroupRouting(group.id, 'all');
    expect(useBotGroupsStore.getState().groups[0].routingMode).toBe('all');
  });

  it('sets a chair only to a current member and clears it when removed', () => {
    const group = useBotGroupsStore.getState().createGroup({
      name: 'Committee',
      memberIds: ['a', 'b', 'c'],
      mode: 'roundtable',
    });
    useBotGroupsStore.getState().setGroupLead(group.id, 'b');
    expect(useBotGroupsStore.getState().groups[0].leadId).toBe('b');

    // A non-member can never chair the room.
    useBotGroupsStore.getState().setGroupLead(group.id, 'outsider');
    expect(useBotGroupsStore.getState().groups[0].leadId).toBeUndefined();

    useBotGroupsStore.getState().setGroupLead(group.id, 'b');
    useBotGroupsStore.getState().setMembers(group.id, ['a', 'c']);
    expect(useBotGroupsStore.getState().groups[0].leadId).toBeNull();
  });
});

describe('group message reactions', () => {
  beforeEach(() => {
    useBotGroupsStore.setState({ groups: [], byGroup: {}, hydrated: true });
  });

  it('counts reactions on the specific message', () => {
    const group = useBotGroupsStore.getState().createGroup({ name: 'Room', memberIds: ['a'] });
    useBotGroupsStore.getState().appendMessage(group.id, {
      id: 'm1', role: 'assistant', content: 'answer', at: '2026-01-01T00:00:00Z',
    });
    useBotGroupsStore.getState().react(group.id, 'm1', '👍');
    useBotGroupsStore.getState().react(group.id, 'm1', '👍');
    expect(useBotGroupsStore.getState().byGroup[group.id][0].reactions).toEqual({ '👍': 2 });
  });
});
