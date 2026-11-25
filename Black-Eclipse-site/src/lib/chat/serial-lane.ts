// ── One serial lane for background network writes ───────────────────────────
// A browser opens ~6 connections per origin. A burst of large background writes
// (a first-run transcript migration can queue hundreds) therefore starves the
// page's own boot fetches, and the app sits on its loading screen while the
// writes trickle. A lane runs the tasks ONE at a time, in order, so a background
// migration can never occupy more than a single connection.
//
// A failed task releases the lane: the next task still runs, and the caller gets
// the rejection (so it can retry on the next change).

export type SerialLane = <T>(task: () => Promise<T>) => Promise<T>;

export function createSerialLane(): SerialLane {
  let tail: Promise<unknown> = Promise.resolve();
  return function enqueue<T>(task: () => Promise<T>): Promise<T> {
    const run = tail.then(task, task);
    tail = run.catch(() => undefined);
    return run;
  };
}
