'use client';

// Gap #8: the coding plan panel. The model records a todo list with
// `code_todo`; this panel shows the SAME persisted list, so the user can see
// what the task intends and how far it has got. It is a view, never a control:
// changing the plan is the model's job.

import { useEffect, useState } from 'react';
import { useT } from '@/lib/i18n/use-t';
import { cn } from '@/lib/utils';
import { getCodeTodos } from '@/lib/harness/clients/code-browser';
import type { CodeTodo, CodeTodoView } from '@/lib/harness/clients/code-browser';

function statusMark(todo: CodeTodo): string {
  if (todo.status === 'completed') return '✓';
  if (todo.status === 'in_progress') return '▸';
  if (todo.status === 'cancelled') return '–';
  return '·';
}

export function CodePlanPanel({ taskId }: { taskId: string }) {
  const t = useT();
  const [view, setView] = useState<CodeTodoView | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const next = await getCodeTodos(taskId);
        if (!alive) return;
        setView(next);
        setFailed(false);
      } catch {
        if (alive) setFailed(true);
      }
    };
    void load();
    // Gap #7b: no polling. The Code screen's run stream dispatches these events.
    const onRunEvent = (event: Event) => {
      const detail = (event as CustomEvent<{ runId?: string }>).detail;
      if (detail?.runId === taskId) void load();
    };
    window.addEventListener('eclipse:code-run-event', onRunEvent);
    return () => {
      alive = false;
      window.removeEventListener('eclipse:code-run-event', onRunEvent);
    };
  }, [taskId]);

  if (failed && !view) return null;
  if (!view) return null;
  const total = view.summary.total;
  if (total === 0) {
    return (
      <div className="glass-card-subtle rounded-xl border border-border/40 p-2.5 text-[11.5px] text-muted-foreground">
        {t('code.plan.title')}: {t('code.plan.empty')}
      </div>
    );
  }
  const done = view.summary.counts.completed ?? 0;
  const active = view.summary.active[0];
  return (
    <div className="glass-card rounded-xl border border-border/40 p-2.5 text-[11.5px]">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-foreground">{t('code.plan.title')}</span>
        <span className="text-muted-foreground">{t('code.plan.progress', { done, total })}</span>
      </div>
      {active && (
        <div className="mt-0.5 text-muted-foreground">{t('code.plan.active', { name: active })}</div>
      )}
      <ul className="mt-1.5 space-y-0.5">
        {view.todos.map((todo, index) => (
          <li
            key={`${index}:${todo.content}`}
            className={cn(
              'flex items-start gap-1.5',
              todo.status === 'completed' && 'text-muted-foreground line-through',
              todo.status === 'in_progress' && 'text-foreground',
              todo.status === 'cancelled' && 'text-muted-foreground/60 line-through',
              todo.status === 'pending' && 'text-muted-foreground',
            )}
          >
            <span className="w-3 shrink-0 text-center">{statusMark(todo)}</span>
            <span className="min-w-0 break-words">{todo.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
