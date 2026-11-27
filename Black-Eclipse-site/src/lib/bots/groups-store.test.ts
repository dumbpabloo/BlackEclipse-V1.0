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
