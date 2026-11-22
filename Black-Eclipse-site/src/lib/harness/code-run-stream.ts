// Gap #7b: live Code-task progress over the runtime's existing SSE stream.
//
// A code task IS a harness run (`runId` is the task id), so the same
// `/api/harness/runs/<id>/stream` proxy the chat uses carries its events. The
// Code screen subscribes while a task is selected; the browser's EventSource
// reconnects on its own and resumes with `Last-Event-ID`.

const EVENT_TYPES = [
  'run.created', 'run.started', 'run.completed', 'run.failed', 'run.cancelled',
  'task.created', 'task.updated', 'task.phase', 'task.completed', 'task.failed',
  'step.started', 'step.completed', 'model.requested', 'model.completed',
  'model.delta', 'tool.requested', 'tool.completed', 'tool.started',
  'approval.requested', 'approval.approved', 'checkpoint.created',
  'checkpoint.restored', 'code.todo.updated', 'code.project_rules_loaded',
  'memory.history_compacted', 'memory.history_bounded',
  'land.planned', 'land.verified', 'land.promoted', 'land.rolled_back',
];

export function subscribeCodeRun(
  runId: string,
  onEvent: (type: string) => void,
): () => void {
  if (typeof window === 'undefined' || !runId) return () => {};
  const source = new EventSource(
    `/api/harness/runs/${encodeURIComponent(runId)}/stream`,
  );
  const handler = (event: Event) => {
    const type = (event as Event).type || 'message';
    onEvent(type);
  };
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, handler as EventListener);
  }
  source.onmessage = handler;
  return () => source.close();
}
