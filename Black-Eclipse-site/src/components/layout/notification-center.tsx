'use client';

// ── Header notification bar ──────────────────────────────────────────────────
// The visible surface of the notification ledger (`lib/notifications/store`).
// The sidebar badges and browser-tab indicator tell you something happened;
// this is where you read WHAT happened, across every producer:
//   price/news alerts · background tasks · approvals · agent runs · bots.
//
// It renders a bell with an unread count in the TopBar and a frosted panel that
// lists the full ledger, filters by kind, and opens what the item points at:
// the original story link when it carries one (news alerts), otherwise the
// owning tab. Opening the panel does NOT mark items read — only selecting one
// (or Mark all read) does, so an operator can scan unread items without losing
// the signal.

import { useCallback, useEffect, useMemo, useRef, useState, type ElementType } from 'react';
import {
  Bell,
  Bot,
  Check,
  CheckCircle2,
  ExternalLink,
  ListChecks,
  ShieldAlert,
  Sparkles,
  X,
} from '@/lib/icons';
import { cn } from '@/lib/utils';
import { openExternal } from '@/lib/desktop/external';
import {
  totalUnread,
  useHydrateNotifications,
  useNotificationsStore,
  type AppNotification,
  type NotificationKind,
} from '@/lib/notifications/store';

type Filter = 'all' | NotificationKind;

const KIND_META: Record<
  NotificationKind,
  { label: string; plural: string; icon: ElementType; accent: string; tint: string }
> = {
  alert: {
    label: 'Alert',
    plural: 'Alerts',
    icon: Bell,
    accent: 'text-amber-300',
    tint: 'border-amber-400/25 bg-amber-400/10',
  },
  task: {
    label: 'Task',
    plural: 'Tasks',
    icon: ListChecks,
    accent: 'text-sky-300',
    tint: 'border-sky-400/25 bg-sky-400/10',
  },
  approval: {
    label: 'Approval',
    plural: 'Approvals',
    icon: ShieldAlert,
    accent: 'text-rose-300',
    tint: 'border-rose-400/25 bg-rose-400/10',
  },
  agent: {
    label: 'Agent',
    plural: 'Agent',
    icon: Sparkles,
    accent: 'text-violet-300',
    tint: 'border-violet-400/25 bg-violet-400/10',
  },
  bot: {
    label: 'Bot',
    plural: 'Bots',
    icon: Bot,
    accent: 'text-emerald-300',
    tint: 'border-emerald-400/25 bg-emerald-400/10',
  },
};

const FILTER_ORDER: NotificationKind[] = ['alert', 'task', 'approval', 'agent', 'bot'];

function timeAgo(ms: number): string {
  const diff = Math.max(0, Date.now() - ms);
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Open what a notification points at: the real source when it carries a link,
 *  otherwise the tab that owns it (the SPA shell listens for that). */
function openNotification(item: AppNotification) {
  const store = useNotificationsStore.getState();
  store.markRead(item.id);
  if (item.url) {
    // The desktop shell owns external links (a new-window request is not hosted).
    if (!openExternal(item.url)) window.open(item.url, '_blank', 'noopener,noreferrer');
    return;
  }
  store.markTabRead(item.tab);
  window.dispatchEvent(new CustomEvent('be:navigate', { detail: { tab: item.tab } }));
}

export function NotificationCenter() {
  useHydrateNotifications();
  const items = useNotificationsStore((s) => s.items);
  const unread = totalUnread(items);

  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<Filter>('all');
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const counts = useMemo(() => {
    const out: Partial<Record<NotificationKind, number>> = {};
    for (const item of items) out[item.kind] = (out[item.kind] ?? 0) + 1;
    return out;
  }, [items]);

  const visible = useMemo(
    () => (filter === 'all' ? items : items.filter((n) => n.kind === filter)),
    [items, filter],
  );

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close(true);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKey);
    panelRef.current?.focus();
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, close]);

  // The panel is only rendered on the client; the trigger itself is stable so
  // SSR and hydration agree (badge starts at 0 until the ledger hydrates).
  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={unread > 0 ? `Notifications — ${unread} unread` : 'Notifications'}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={unread > 0 ? `${unread} unread notification${unread === 1 ? '' : 's'}` : 'Notifications'}
        className={cn(
          'relative flex h-10 w-10 items-center justify-center rounded-xl transition-colors cursor-pointer',
          open
            ? 'bg-white/[0.08] text-primary'
            : 'text-muted-foreground/70 hover:bg-accent hover:text-foreground',
        )}
      >
        <Bell size={18} weight="regular" />
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full border border-background bg-rose-500 px-1 text-[9px] font-bold leading-none text-white">
            {unread > 9 ? '9+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          ref={panelRef}
          role="dialog"
          aria-label="Notifications"
          tabIndex={-1}
          className="absolute right-0 top-[calc(100%+0.5rem)] z-[140] w-[420px] max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-2xl border border-white/10 dark:border-white/5 glass-card-strong shadow-2xl outline-none animate-fade-in-up"
        >
          {/* Header */}
          <div className="flex items-center gap-2 border-b border-border/20 px-4 py-3">
            <Bell size={14} className="text-primary" />
            <div className="min-w-0">
              <p className="text-[12px] font-semibold text-foreground">Notifications</p>
              <p className="text-[9px] font-mono text-muted-foreground">
                {unread > 0 ? `${unread} unread` : 'all read'} · {items.length} total
              </p>
            </div>
            <div className="ml-auto flex items-center gap-1">
              {unread > 0 && (
                <button
                  type="button"
                  onClick={() => useNotificationsStore.getState().markAllRead()}
                  className="inline-flex items-center gap-1 rounded-lg border border-border/40 px-2 py-1 text-[10px] font-semibold text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
                  title="Mark every notification read"
                >
                  <Check size={10} /> Mark all read
                </button>
              )}
              <button
                type="button"
                onClick={() => close(true)}
                aria-label="Close notifications"
                className="flex size-7 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground cursor-pointer"
              >
                <X size={12} />
              </button>
            </div>
          </div>

          {/* Kind filters — always offered, so every producer's lane is
              discoverable even before its first event (count 0). */}
          <div className="flex flex-wrap items-center gap-1 border-b border-border/20 px-3 py-2">
            <FilterChip
              active={filter === 'all'}
              label={`All${items.length ? ` (${items.length})` : ''}`}
              onClick={() => setFilter('all')}
            />
            {FILTER_ORDER.map((kind) => (
              <FilterChip
                key={kind}
                active={filter === kind}
                label={`${KIND_META[kind].plural} (${counts[kind] ?? 0})`}
                onClick={() => setFilter(kind)}
              />
            ))}
          </div>

          {/* Ledger */}
          <div className="max-h-[min(60vh,440px)] overflow-y-auto overscroll-contain p-2">
            {visible.length === 0 ? (
              <div className="flex flex-col items-center gap-2 px-6 py-10 text-center">
                <span className="flex size-10 items-center justify-center rounded-full border border-border/30 bg-accent/30">
                  <CheckCircle2 size={18} className="text-emerald-300" />
                </span>
                <p className="text-xs font-semibold text-foreground">
                  {items.length === 0 ? 'No notifications yet' : 'Nothing in this filter'}
                </p>
                <p className="text-[10px] text-muted-foreground">
                  Fired alerts, background tasks, approvals and agent runs appear here.
                </p>
              </div>
            ) : (
              <ul className="space-y-1">
                {visible.map((item) => {
                  const meta = KIND_META[item.kind] ?? KIND_META.alert;
                  const Icon = meta.icon;
                  return (
                    <li key={item.id}>
                      <button
                        type="button"
                        onClick={() => {
                          openNotification(item);
                          close();
                        }}
                        className={cn(
                          'group flex w-full items-start gap-2.5 rounded-xl border border-transparent px-2.5 py-2.5 text-left transition-colors cursor-pointer hover:border-border/30 hover:bg-accent/40',
                          !item.read && 'bg-accent/20',
                        )}
                      >
                        <span
                          className={cn(
                            'mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg border',
                            meta.tint,
                          )}
                        >
                          <Icon size={13} className={meta.accent} weight="regular" />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-start gap-1.5">
                            <span className="min-w-0 flex-1 whitespace-normal break-words text-[11px] font-semibold leading-snug text-foreground">
                              {item.title}
                            </span>
                            {!item.read && (
                              <span
                                aria-label="Unread"
                                className="mt-1 size-1.5 shrink-0 rounded-full bg-rose-400"
                              />
                            )}
                            <span className="shrink-0 pt-px text-[9px] font-mono text-muted-foreground">
                              {timeAgo(item.at)}
                            </span>
                          </span>
                          {item.body && (
                            <span className="mt-0.5 line-clamp-2 block text-[10px] leading-snug text-muted-foreground">
                              {item.body}
                            </span>
                          )}
                          <span className="mt-1 inline-flex items-center gap-1 text-[9px] font-mono uppercase tracking-wider text-muted-foreground/70">
                            {meta.label} · {item.tab}
                            {item.url && (
                              <>
                                <ExternalLink size={9} weight="bold" />
                                <span className="normal-case tracking-normal">source</span>
                              </>
                            )}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between gap-2 border-t border-border/20 px-4 py-2">
            <span className="text-[9px] font-mono text-muted-foreground">
              History survives reloads · capped at 100
            </span>
            <button
              type="button"
              onClick={() => {
                window.dispatchEvent(new CustomEvent('be:navigate', { detail: { tab: 'alerts' } }));
                useNotificationsStore.getState().markTabRead('alerts');
                close();
              }}
              className="text-[10px] font-semibold text-primary transition-colors hover:text-primary/80 cursor-pointer"
            >
              Open Alerts
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function FilterChip({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'rounded-full border px-2 py-0.5 text-[9px] font-semibold transition-colors cursor-pointer',
        active
          ? 'border-primary/40 bg-primary/15 text-primary'
          : 'border-border/30 text-muted-foreground hover:border-primary/30 hover:text-foreground',
      )}
    >
      {label}
    </button>
  );
}
