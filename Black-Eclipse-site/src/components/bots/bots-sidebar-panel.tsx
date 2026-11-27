'use client';

// The sidebar's bot list — shown while the Bots mode is active, exactly as chat
// history is shown under ChatBot. Lists the user's REAL bots (the shared bot
// store) plus their group chats, and opens one on click. Collapsed to avatars
// when the sidebar is narrow. This is the ONE bot list: the Bots page itself is
// just the conversation + the scheduled-jobs rail.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import { Archive, CalendarDays, Copy, MoreHorizontal, Pencil, Pin, PinOff, Plus, RefreshCw, Search, Trash2, Upload, Users } from '@/lib/icons';
import { BotAvatar } from '@/components/bots/bot-avatar';
import { BotsTextSizeMenu } from '@/components/bots/bots-text-size';
import { GhostAvatar } from '@/components/bots/ghost-avatar';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useBotsStore } from '@/lib/bots/bots-store';
import { avatarFor, useBotAvatarStore, useHydrateBotAvatars } from '@/lib/bots/avatar-store';
import { useBotChatStore } from '@/lib/bots/chat-store';
import { isDesk } from '@/lib/bots/desk';
import { useBotGroupsStore, useHydrateBotGroups } from '@/lib/bots/groups-store';
import { useWorkspaceStore } from '@/lib/agent-workspace/store';
import { duplicateBot } from '@/lib/bots/duplicate';
import { importBotInput } from '@/lib/bots/transfer';
import { forgetBotLocally, forgetBotMemory } from '@/lib/bots/purge';
import { ghostSpecFromId } from '@/lib/bots/ghost';
import { botMatchesSearch, executionLabel, isTerminalExecution } from '@/lib/bots/state';
import { archiveAutomation, createAutomation, deleteAutomation, updateAutomation } from '@/lib/harness/clients/automation-browser';
import type { AutomationStatusSummaryView, AutomationView } from '@/lib/harness/clients/automation-browser';
import type { BotMessage } from '@/lib/bots/chat-store';

/** Most-recent activity for a bot: its last chat message, else its last run,
 *  else its last edit. Used to sort the list newest-first. */
function lastActivityMs(
  botId: string,
  fallback: string | undefined | null,
  byBot: Record<string, BotMessage[]>,
  statusById: Record<string, AutomationStatusSummaryView>,
): number {
  const msgs = byBot[botId];
  const lastMsg = msgs && msgs.length > 0 ? Date.parse(msgs[msgs.length - 1].at) : 0;
  const last = statusById[botId]?.lastExecution;
  const runAt = last ? Date.parse(last.finishedAt ?? last.startedAt) : 0;
  const edited = fallback ? Date.parse(fallback) : 0;
  return Math.max(lastMsg || 0, runAt || 0, edited || 0);
}

export interface BotsSidebarPanelProps {
  collapsed: boolean;
  /** Switch to the Bots tab (the store selection is set here, too). */
  onActivate: () => void;
}

export function BotsSidebarPanel({ collapsed, onActivate }: BotsSidebarPanelProps) {
  const t = useT();
  useHydrateBotAvatars();
  useHydrateBotGroups();
  const bots = useBotsStore((s) => s.bots);
  const loading = useBotsStore((s) => s.loading);
  const selectedId = useBotsStore((s) => s.selectedId);
  const selectedGroupId = useBotsStore((s) => s.selectedGroupId);
  const statusById = useBotsStore((s) => s.statusById);
  const seenRuns = useBotsStore((s) => s.seenRuns);
  const select = useBotsStore((s) => s.select);
  const selectGroup = useBotsStore((s) => s.selectGroup);
  const hydrate = useBotsStore((s) => s.hydrate);
  const pinnedIds = useBotsStore((s) => s.pinnedIds);
  const togglePin = useBotsStore((s) => s.togglePin);
  const forget = useBotsStore((s) => s.forget);
  const release = useBotsStore((s) => s.release);
  const openCreate = useBotsStore((s) => s.openCreate);
  const openGroupCreate = useBotsStore((s) => s.openGroupCreate);
  const avatars = useBotAvatarStore((s) => s.avatars);
  const groups = useBotGroupsStore((s) => s.groups);
  const byGroup = useBotGroupsStore((s) => s.byGroup);
  const schedulerOpen = useWorkspaceStore((s) => s.open);
  const toggleScheduler = useWorkspaceStore((s) => s.toggle);
  const statusLoaded = useBotsStore((s) => s.statusLoaded);
  const byBot = useBotChatStore((s) => s.byBot);
  const [deleteTarget, setDeleteTarget] = useState<AutomationView | null>(null);
  const [query, setQuery] = useState('');
  const importRef = useRef<HTMLInputElement>(null);
  const [renameTarget, setRenameTarget] = useState<AutomationView | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // The Bots workspace already refreshes the shared store on a 20s interval
  // while this panel is visible; one hydrate on mount is enough here.
  useEffect(() => {
    void hydrate();
  }, [hydrate]);

  // A bot has an unseen message when its latest terminal run is newer than the
  // last one the operator opened. A bot never opened before is not "unread"
  // (its history is seeded silently below), so no dot flashes on first load.
  const isUnread = useCallback(
    (botId: string) => {
      const last = statusById[botId]?.lastExecution;
      if (!last || !isTerminalExecution(last.state)) return false;
      const seen = seenRuns[botId];
      // No seen entry means this bot had no history at the first status sweep
      // (created later, or never run) — its first completed run is unread.
      if (seen === undefined) return statusLoaded;
      return seen !== last.id;
    },
    [statusById, seenRuns, statusLoaded],
  );

  // Pinned bots first (newest pin first), then most-recent activity.
  const sortedBots = useMemo(() => {
    const rank = new Map(pinnedIds.map((id, index) => [id, index]));
    return [...bots].sort((a, b) => {
      const ar = rank.get(a.id);
      const br = rank.get(b.id);
      if (ar !== undefined || br !== undefined) {
        if (ar === undefined) return 1;
        if (br === undefined) return -1;
        return ar - br;
      }
      return (
        lastActivityMs(b.id, b.lastRunAt ?? b.updatedAt, byBot, statusById)
        - lastActivityMs(a.id, a.lastRunAt ?? a.updatedAt, byBot, statusById)
      );
    });
  }, [bots, byBot, statusById, pinnedIds]);

  const visibleBots = useMemo(
    () => sortedBots.filter((bot) => botMatchesSearch(bot, query)),
    [sortedBots, query],
  );

  const sortedGroups = useMemo(
    () => [...groups].sort((a, b) => {
      const am = byGroup[a.id];
      const bm = byGroup[b.id];
      const at = am && am.length > 0 ? Date.parse(am[am.length - 1].at) : Date.parse(a.createdAt);
      const bt = bm && bm.length > 0 ? Date.parse(bm[bm.length - 1].at) : Date.parse(b.createdAt);
      return (bt || 0) - (at || 0);
    }),
    [groups, byGroup],
  );

  const visibleGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? sortedGroups.filter((g) => g.name.toLowerCase().includes(q)) : sortedGroups;
  }, [sortedGroups, query]);

  const open = (id: string | null) => {
    select(id);
    onActivate();
  };

  const openGroup = (id: string) => {
    selectGroup(id);
    onActivate();
  };

  const startCreate = () => {
    openCreate();
    onActivate();
  };

  const startGroupCreate = () => {
    openGroupCreate();
    onActivate();
  };

  const startRename = (bot: AutomationView) => {
    setActionError(null);
    setRenameError(null);
    setRenameValue(bot.name);
    setRenameTarget(bot);
  };

  const confirmRename = async () => {
    if (!renameTarget || renaming) return;
    const name = renameValue.trim();
    if (!name || name === renameTarget.name) {
      setRenameTarget(null);
      return;
    }
    setRenaming(true);
    setRenameError(null);
    try {
      await updateAutomation(renameTarget.id, { name });
      setRenameTarget(null);
      await hydrate();
    } catch (err) {
      setRenameError(err instanceof Error ? err.message : 'rename failed');
    } finally {
      setRenaming(false);
    }
  };

  const duplicate = async (bot: AutomationView) => {
    setActionError(null);
    try {
      const created = await duplicateBot(bot);
      await hydrate();
      open(created.id);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'duplicate failed');
    }
  };

  const archive = async (bot: AutomationView) => {
    setActionError(null);
    if (selectedId === bot.id) select(null);
    try {
      await archiveAutomation(bot.id);
      // Archived bots stop running but their history and conversation stay.
      // Reconcile from the server so the row reflects the real state.
      await hydrate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'archive failed');
      await hydrate();
    }
  };

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    setActionError(null);
    try {
      const parsed = JSON.parse(await file.text()) as unknown;
      const input = importBotInput(parsed);
      const created = await createAutomation(input);
      await hydrate();
      open(created.id);
    } catch (err) {
      setActionError(
        err instanceof Error
          ? `${t('bots.importFailed')} ${err.message}`
          : t('bots.importFailed'),
      );
    } finally {
      if (importRef.current) importRef.current.value = '';
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    const id = deleteTarget.id;
    // Remove the row and close the dialog before the request: the runtime can
    // take seconds to answer, and the operator must not be blocked meanwhile.
    setActionError(null);
    setDeleteTarget(null);
    if (selectedId === id) select(null);
    forget(id);
    try {
      await deleteAutomation(id);
      // Reconcile in the background; `forget` keeps it out of any hydrate.
      void forgetBotMemory(id);
      forgetBotLocally(id);
      await hydrate();
    } catch (err) {
      // The delete failed — bring the bot back from the server's list.
      release(id);
      setActionError(err instanceof Error ? err.message : 'delete failed');
      await hydrate();
    }
  };

  if (collapsed) {
    return (
      <div className="flex w-full flex-col items-center gap-1.5 py-2">
        <button
          type="button"
          onClick={startCreate}
          className="flex h-9 w-9 items-center justify-center rounded-xl text-muted-foreground hover:bg-accent hover:text-foreground"
          title={t('bots.dialog.title')}
          aria-label={t('bots.dialog.title')}
        >
          <Plus size={16} />
        </button>
        <button
          type="button"
          onClick={toggleScheduler}
          className={cn(
            'flex h-9 w-9 items-center justify-center rounded-xl text-muted-foreground hover:bg-accent hover:text-foreground',
            schedulerOpen && 'bg-white/[0.08] text-foreground',
          )}
          title={t('bots.jobsPanel.open')}
          aria-label={t('bots.jobsPanel.open')}
          aria-pressed={schedulerOpen}
        >
          <CalendarDays size={16} />
        </button>
        {sortedBots.slice(0, 12).map((bot) => {
          const active = bot.id === selectedId;
          return (
            <button
              key={bot.id}
              type="button"
              onClick={() => open(bot.id)}
              title={bot.name}
              aria-label={bot.name}
              className={cn(
                'relative flex h-9 w-9 items-center justify-center rounded-xl transition-colors',
                active ? 'bg-white/[0.08]' : 'hover:bg-accent/60',
              )}
            >
              <BotAvatar botId={bot.id} spec={avatarFor(avatars, bot.id)} size={24} title={bot.name} />
              {isUnread(bot.id) && (
                <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-primary ring-2 ring-background" />
              )}
            </button>
          );
        })}
        {sortedGroups.slice(0, 6).map((g) => (
          <button
            key={g.id}
            type="button"
            onClick={() => openGroup(g.id)}
            title={g.name}
            aria-label={g.name}
            className={cn(
              'relative flex h-9 w-9 items-center justify-center rounded-xl transition-colors',
              g.id === selectedGroupId ? 'bg-white/[0.08]' : 'hover:bg-accent/60',
            )}
          >
            {g.image ? (
              <img src={g.image} alt="" className="h-6 w-6 rounded-full object-cover" />
            ) : (
              <GhostAvatar spec={ghostSpecFromId(g.id)} size={24} />
            )}
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="bots-text-scope flex min-h-0 flex-col">
      <div className="mb-2 flex items-center justify-between gap-2 px-1">
        <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
          {t('bots.title')} · {bots.length}
        </span>
        <div className="flex items-center gap-0.5">
          <BotsTextSizeMenu />
          <button
            type="button"
            onClick={toggleScheduler}
            className={cn(
              'rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground',
              schedulerOpen && 'bg-white/[0.08] text-foreground',
            )}
            title={t('bots.jobsPanel.open')}
            aria-label={t('bots.jobsPanel.open')}
            aria-pressed={schedulerOpen}
          >
            <CalendarDays size={13} />
          </button>
          <button
            type="button"
            onClick={startCreate}
            className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={t('bots.dialog.title')}
            aria-label={t('bots.dialog.title')}
          >
            <Plus size={13} />
          </button>
          <button
            type="button"
            onClick={() => importRef.current?.click()}
            className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={t('bots.import')}
            aria-label={t('bots.import')}
          >
            <Upload size={13} />
          </button>
          <input
            ref={importRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => void importFile(e.target.files?.[0])}
          />
          <button
            type="button"
            onClick={() => void hydrate()}
            className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={t('common.refresh')}
            aria-label={t('common.refresh')}
          >
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      {actionError && (
        <p className="mx-1 mb-2 rounded-lg border border-rose-500/30 bg-rose-500/5 px-2 py-1.5 text-[10px] text-rose-300">
          {actionError}
        </p>
      )}

      <div className="relative mx-1 mb-2">
        <Search size={12} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('common.search')}
          aria-label={t('common.search')}
          className="h-7 pl-7 text-[11px]"
        />
      </div>

      {loading && bots.length === 0 ? (
        <p className="px-2 py-1 text-xs text-muted-foreground">{t('common.loading')}</p>
      ) : bots.length === 0 ? (
        <button
          type="button"
          onClick={startCreate}
          className="mx-1 rounded-xl border border-dashed border-border/50 px-3 py-4 text-left text-xs text-muted-foreground hover:border-border hover:text-foreground"
        >
          {t('bots.empty')}
        </button>
      ) : (
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto scrollbar-hide">
          {visibleBots.map((bot) => {
            const active = bot.id === selectedId;
            const pinned = pinnedIds.includes(bot.id);
            const live = statusById[bot.id]?.lastExecution;
            const unread = isUnread(bot.id);
            const subtitle = live
              ? executionLabel(live.state)
              : isDesk(bot)
                ? 'desk'
                : bot.state === 'active'
                  ? t('bots.state.active')
                  : bot.state;
            return (
              <li
                key={bot.id}
                className={cn(
                  'group flex items-center gap-0.5 rounded-xl pr-1 transition-colors',
                  active ? 'bg-white/[0.08] text-foreground' : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
                )}
              >
                <button
                  type="button"
                  onClick={() => open(bot.id)}
                  className="flex min-w-0 flex-1 items-center gap-2.5 rounded-xl px-2 py-1.5 text-left"
                >
                  <BotAvatar botId={bot.id} spec={avatarFor(avatars, bot.id)} size={26} title={bot.name} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1">
                      <span className={cn('truncate text-xs', unread ? 'font-semibold text-foreground' : 'font-semibold')}>{bot.name}</span>
                      {pinned && <Pin size={10} className="shrink-0 text-primary/70" aria-label={t('sidebar.pinned')} />}
                    </span>
                    <span className="block truncate text-[10px] capitalize opacity-80">{subtitle}</span>
                  </span>
                  {unread && <span className="h-2 w-2 shrink-0 rounded-full bg-primary" aria-hidden />}
                </button>
                <DropdownMenu>
                  <DropdownMenuTrigger
                    render={
                      <button
                        type="button"
                        className="shrink-0 rounded-md p-1 text-muted-foreground opacity-60 transition-opacity hover:bg-accent hover:text-foreground hover:opacity-100 focus-visible:opacity-100 data-[popup-open]:opacity-100 max-md:opacity-100"
                        aria-label={t('bots.more')}
                        title={t('bots.more')}
                      />
                    }
                  >
                    <MoreHorizontal size={14} />
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" alignOffset={6} sideOffset={6} className="w-48">
                    <DropdownMenuItem onClick={() => togglePin(bot.id)}>
                      {pinned ? <PinOff size={14} /> : <Pin size={14} />}
                      {pinned ? t('sidebar.unpin') : t('sidebar.pin')}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => startRename(bot)}>
                      <Pencil size={14} /> {t('sidebar.rename')}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => void duplicate(bot)}>
                      <Copy size={14} /> {t('bots.duplicate')}
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => void archive(bot)}>
                      <Archive size={14} /> {t('bots.archive')}
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem variant="destructive" onClick={() => setDeleteTarget(bot)}>
                      <Trash2 size={14} /> {t('bots.delete')}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </li>
            );
          })}
        </ul>
      )}

      {/* Group chats */}
      <div className="mt-3 border-t border-border/10 pt-2">
        <div className="mb-1 flex items-center justify-between gap-2 px-1">
          <span className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            <Users size={11} /> {t('bots.groups.title')}
          </span>
          <button
            type="button"
            onClick={startGroupCreate}
            className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={t('bots.groups.new')}
            aria-label={t('bots.groups.new')}
          >
            <Plus size={12} />
          </button>
        </div>
        {groups.length === 0 ? (
          <button
            type="button"
            onClick={startGroupCreate}
            className="mx-1 w-[calc(100%-0.5rem)] rounded-xl border border-dashed border-border/40 px-2 py-2 text-left text-[10px] text-muted-foreground hover:border-border hover:text-foreground"
          >
            {t('bots.groups.empty')}
          </button>
        ) : (
          <ul className="space-y-0.5">
            {visibleGroups.map((g) => {
              const active = g.id === selectedGroupId;
              return (
                <li key={g.id}>
                  <button
                    type="button"
                    onClick={() => openGroup(g.id)}
                    className={cn(
                      'flex w-full items-center gap-2.5 rounded-xl px-2 py-1.5 text-left transition-colors',
                      active ? 'bg-white/[0.08] text-foreground' : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
                    )}
                  >
                    {g.image ? (
                      <img src={g.image} alt="" className="h-6 w-6 shrink-0 rounded-full object-cover" />
                    ) : (
                      <GhostAvatar spec={ghostSpecFromId(g.id)} size={26} />
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-semibold">{g.name}</span>
                      <span className="block truncate text-[10px] opacity-80">
                        {t('bots.groups.members', { count: g.memberIds.length })}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <Dialog
        open={Boolean(renameTarget)}
        onOpenChange={(next) => {
          if (!next && !renaming) {
            setRenameTarget(null);
            setRenameError(null);
          }
        }}
      >
        <DialogContent>
          <form
            className="grid gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              void confirmRename();
            }}
          >
            <DialogHeader>
              <DialogTitle>{t('sidebar.rename')}</DialogTitle>
              <DialogDescription>{renameTarget?.name ?? ''}</DialogDescription>
            </DialogHeader>
            <Input
              autoFocus
              value={renameValue}
              onChange={(event) => setRenameValue(event.target.value)}
              placeholder={t('bots.namePlaceholder')}
              aria-label={t('bots.namePlaceholder')}
            />
            {renameError && (
              <p className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-300">
                {renameError}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="ghost" disabled={renaming} onClick={() => setRenameTarget(null)}>
                {t('bots.cancel')}
              </Button>
              <Button type="submit" disabled={renaming || !renameValue.trim()}>
                {renaming ? t('bots.saving') : t('bots.save')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(deleteTarget)}
        onOpenChange={(next) => {
          if (!next) setDeleteTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('bots.delete')}</DialogTitle>
            <DialogDescription>
              {t('bots.deleteConfirm')} {deleteTarget ? `“${deleteTarget.name}”` : ''}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)}>
              {t('bots.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => void confirmDelete()}>
              {t('bots.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
