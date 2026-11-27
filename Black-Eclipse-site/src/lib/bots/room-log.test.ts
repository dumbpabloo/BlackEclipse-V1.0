import { describe, expect, it } from 'vitest';

import { mergeRooms } from './room-log';

interface Room {
  id: string;
  name: string;
  updatedAt?: string;
}

describe('mergeRooms', () => {
  it('restores a server room that is not local (cleared snapshot)', () => {
    const merged = mergeRooms<Room>([], [
      { id: 'r1', name: 'Committee', updatedAt: '2026-11-26T10:00:00Z' },
    ]);
    expect(merged).toEqual([
      { id: 'r1', name: 'Committee', updatedAt: '2026-11-26T10:00:00Z' },
    ]);
  });

  it('lets the newer write win and keeps a local-only room', () => {
    const merged = mergeRooms<Room>(
      [
        { id: 'r1', name: 'Local newer', updatedAt: '2026-11-26T11:00:00Z' },
        { id: 'r2', name: 'Local only' },
      ],
      [
        { id: 'r1', name: 'Server older', updatedAt: '2026-11-26T10:00:00Z' },
        { id: 'r3', name: 'Server only', updatedAt: '2026-11-26T09:00:00Z' },
      ],
    );
    expect(merged.find((room) => room.id === 'r1')?.name).toBe('Local newer');
    expect(merged.find((room) => room.id === 'r2')?.name).toBe('Local only');
    expect(merged.find((room) => room.id === 'r3')?.name).toBe('Server only');
  });

  it('adopts a newer server write', () => {
    const merged = mergeRooms<Room>(
      [{ id: 'r1', name: 'Local older', updatedAt: '2026-11-26T10:00:00Z' }],
      [{ id: 'r1', name: 'Server newer', updatedAt: '2026-11-26T12:00:00Z' }],
    );
    expect(merged[0].name).toBe('Server newer');
  });
});
