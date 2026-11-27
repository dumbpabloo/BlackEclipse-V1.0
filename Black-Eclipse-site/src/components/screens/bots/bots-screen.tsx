'use client';

// ── Bots workspace ───────────────────────────────────────────────────────────
// The conversation IS the page: a full-height chat with a bot (or a group room).
// The bot list AND group chats live in the LEFT app sidebar (Bots mode). The
// scheduled-jobs scheduler is the right-docked Agent Workspace slot (see
// `components/bots/scheduler-dock.tsx`), opened from the left sidebar. Every
// control reads and writes REAL state via `/api/automations`.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useT } from '@/lib/i18n';
import { Ghost, Plus, Users } from '@/lib/icons';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { BotChat } from '@/components/bots/bot-chat';
import { CreateBotDialog } from '@/components/bots/create-bot-dialog';
import { DeskBuilderDialog } from '@/components/bots/desk-builder-dialog';
import { DeskRoom } from '@/components/bots/desk-room';
import { GroupChatDialog } from '@/components/bots/group-chat-dialog';
import { GroupChatRoom } from '@/components/bots/group-chat-room';
import { InstitutionRunDialog } from '@/components/bots/institution-run-dialog';
import {
  deleteAutomation, runAutomationNow,
  type AutomationView,
} from '@/lib/harness/clients/automation-browser';
import { isDesk } from '@/lib/bots/desk';
import { duplicateBot } from '@/lib/bots/duplicate';
import { useSeedFinancePack } from '@/lib/bots/finance-bots';
import { useSeedEur1mBot } from '@/lib/bots/trading/eur-1m-seed';import { forgetBotLocally, forgetBotMemory } from '@/lib/bots/purge';
import { useBotsStore } from '@/lib/bots/bots-store';
import { useHydrateBotAvatars } from '@/lib/bots/avatar-store';
import { useBotGroupsStore, useHydrateBotGroups } from '@/lib/bots/groups-store';
import { canHandleKeyboardEvent } from '@/lib/keyboard';

export function BotsScreen({ active = true }: { active?: boolean }) {
  const t = useT();
  useHydrateBotAvatars();
  useHydrateBotGroups();

  const bots = useBotsStore((s) => s.bots);
  const error = useBotsStore((s) => s.error);
  const loading = useBotsStore((s) => s.loading);
  const selectedId = useBotsStore((s) => s.selectedId);
  const selectedGroupId = useBotsStore((s) => s.selectedGroupId);
  const hydrate = useBotsStore((s) => s.hydrate);
  const select = useBotsStore((s) => s.select);
  const selectGroup = useBotsStore((s) => s.selectGroup);
  const forget = useBotsStore((s) => s.forget);
  const release = useBotsStore((s) => s.release);
  const createOpen = useBotsStore((s) => s.createOpen);
  const openCreate = useBotsStore((s) => s.openCreate);
  const setCreateOpen = useBotsStore((s) => s.setCreateOpen);
  const groupCreateOpen = useBotsStore((s) => s.groupCreateOpen);
  const openGroupCreate = useBotsStore((s) => s.openGroupCreate);
  const setGroupCreateOpen = useBotsStore((s) => s.setGroupCreateOpen);

  const groups = useBotGroupsStore((s) => s.groups);
  const createGroup = useBotGroupsStore((s) => s.createGroup);
  const financePack = useSeedFinancePack();
  const eurBot = useSeedEur1mBot();

  const [busyId, setBusyId] = useState<string | null>(null);
  const [institutionOpen, setInstitutionOpen] = useState(false);
  const [deskOpen, setDeskOpen] = useState(false);
  const [editDesk, setEditDesk] = useState<AutomationView | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AutomationView | null>(null);
  const [runKey, setRunKey] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const didAutoSelect = useRef(false);

  // Initial load, then a slow refresh ONLY while Bots is the visible tab. The
  // keep-alive mount means this component stays alive on every other tab; an
  // unconditional interval kept fanning out list + status requests forever.
  useEffect(() => {
    void hydrate();
    if (!active) return;
    const timer = setInterval(() => void hydrate(), 20000);
    return () => clearInterval(timer);
  }, [hydrate, active]);

  // Deep link: /?tab=bots&bot=<id> or /?tab=bots&group=<id> (read once on mount).
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const params = new URLSearchParams(window.location.search);
    const bot = params.get('bot');
    const group = params.get('group');
    if (bot) select(bot);
    else if (group) selectGroup(group);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Default the page to the most recent conversation. Runs once, after the list
  // loads, and never overrides an explicit deep link or selection.
  useEffect(() => {
    if (didAutoSelect.current || loading) return;
    if (typeof window !== 'undefined') {
      const params = new URLSearchParams(window.location.search);
      if (params.get('bot') || params.get('group')) {
        didAutoSelect.current = true;
        return;
      }
    }
    if (selectedId || selectedGroupId) {
      didAutoSelect.current = true;
      return;
    }
    if (bots.length === 0) return;
    didAutoSelect.current = true;
    const recent = [...bots].sort((a, b) => recency(b) - recency(a))[0];
    select(recent.id);
  }, [bots, loading, selectedId, selectedGroupId, select]);

  const selectedBot = useMemo(() => bots.find((b) => b.id === selectedId) ?? null, [bots, selectedId]);
  const selectedGroup = useMemo(() => groups.find((g) => g.id === selectedGroupId) ?? null, [groups, selectedGroupId]);

  // Escape backs out of an open conversation to the Bots overview (the page
  // with the New bot / New group buttons), mirroring a back button. Blocked
  // while typing, while a dialog/sheet is open (it owns Escape first), and when
  // nothing is selected (the overview is already showing).
  useEffect(() => {
    if (!selectedId && !selectedGroupId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !canHandleKeyboardEvent(event)) return;
      if (selectedGroupId) selectGroup(null);
      else select(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selectedId, selectedGroupId, select, selectGroup]);

  const runAction = useCallback(
    async (id: string, fn: () => Promise<unknown>): Promise<boolean> => {
      setBusyId(id);
      setActionError(null);
      try {
        await fn();
        await hydrate();
        return true;
      } catch (err) {
        setActionError(err instanceof Error ? err.message : 'action failed');
        return false;
      } finally {
        setBusyId(null);
      }
    },
    [hydrate],
  );

  const runNow = useCallback(
    (bot: AutomationView) => {
      setRunKey((k) => k + 1);
      return runAction(bot.id, () => runAutomationNow(bot.id));
    },
    [runAction],
  );

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return;
    const id = deleteTarget.id;
    setDeleteTarget(null);
    if (selectedId === id) select(null);
    // Drop the row immediately and keep any in-flight hydrate from resurrecting
    // it; a failed delete restores it from the server's list.
    forget(id);
    const ok = await runAction(id, () => deleteAutomation(id));
    if (ok) {
      // The durable definition is gone; clear the client-side thread/face/room
      // membership now and purge the private memory buckets best-effort.
      forgetBotLocally(id);
      void forgetBotMemory(id);
    } else {
      release(id);
      await hydrate();
    }
  }, [deleteTarget, selectedId, runAction, select, forget, release, hydrate]);

  const handleGroupCreate = useCallback(
    (input: { name: string; memberIds: string[]; image?: string }) => {
      const group = createGroup(input);
      setGroupCreateOpen(false);
      selectGroup(group.id);
    },
    [createGroup, setGroupCreateOpen, selectGroup],
  );

  const handleCreated = useCallback(
    (bot: AutomationView) => {
      setCreateOpen(false);
      void hydrate();
      select(bot.id);
    },
    [setCreateOpen, hydrate, select],
  );

  // Templates render inside the Create Bot dialog (one entry point, not six
  // buttons on an empty page). The dialog closes first so the destination
  // surface (desk builder, committee, seeded room) is not stacked on top of it.
  const useTemplate = useCallback(
    (template: 'desk' | 'institution' | 'finance' | 'eur1m') => {
      setCreateOpen(false);
      if (template === 'desk') {
        setEditDesk(null);
        setDeskOpen(true);
        return;
      }
      if (template === 'institution') {
        setInstitutionOpen(true);
        return;
      }
      if (template === 'finance') {
        void financePack.seed();
        return;
      }
      void eurBot.seed();
    },
    [financePack, eurBot, setCreateOpen],
  );

  return (
    <div className="bots-text-scope flex h-full min-h-0 w-full flex-col">
      {(actionError || error) && (
        <div className="glass-card-subtle mx-2 mt-2 flex items-center gap-2 rounded-lg border border-rose-500/30 px-3 py-2 text-sm text-rose-300 md:mx-3">
          <span className="flex-1">{actionError || error}</span>
          <button type="button" onClick={() => { setActionError(null); void hydrate(); }} className="text-xs underline">
            {t('common.refresh')}
          </button>
        </div>
      )}

      {/* The conversation — full page. */}
      <div className="min-h-0 flex-1 p-2 md:p-3">
        {selectedGroup ? (
          <GroupChatRoom
            key={selectedGroup.id}
            group={selectedGroup}
            bots={bots}
            active={active}
            onDeleted={() => selectGroup(null)}
            onChanged={() => void hydrate()}
          />
        ) : selectedBot ? (
          isDesk(selectedBot) ? (
            <DeskRoom
              key={selectedBot.id}
              bot={selectedBot}
              onClose={() => select(null)}
              onChanged={() => void hydrate()}
              onEdit={(desk) => {
                setEditDesk(desk);
                setDeskOpen(true);
              }}
            />
          ) : (
            <BotChat
              key={selectedBot.id}
              bot={selectedBot}
              active={active}
              busy={busyId === selectedBot.id}
              onClose={() => select(null)}
              onChanged={() => void hydrate()}
              onRunNow={(bot) => runNow(bot)}
              refreshKey={runKey}
              onDuplicate={(bot) => runAction(bot.id, () => duplicateBot(bot))}
              onDelete={(bot) => setDeleteTarget(bot)}
            />
          )
        ) : (
          <CenterEmpty
            firstRun={bots.length === 0 && groups.length === 0}
            loading={loading}
            onCreate={openCreate}
            onCreateGroup={openGroupCreate}
          />
        )}
      </div>

      {/* New bot — avatar picker + identity, opened from the header and sidebar. */}
      <CreateBotDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        existingNames={bots.map((b) => b.name)}
        bots={bots}
        onCreated={handleCreated}
        onUseTemplate={useTemplate}
      />

      {/* Institution committee — seeds the seats and runs the durable pipeline. */}
      <InstitutionRunDialog
        open={institutionOpen}
        onOpenChange={setInstitutionOpen}
        existingBots={bots}
        onSeeded={() => void hydrate()}
      />

      {/* New / edit desk — a user-authored team run stored on a bot. */}
      <DeskBuilderDialog
        open={deskOpen}
        onOpenChange={(open) => {
          setDeskOpen(open);
          if (!open) setEditDesk(null);
        }}
        desk={editDesk}
        bots={bots}
        onSaved={(saved) => {
          void hydrate();
          select(saved.id);
        }}
      />

      {/* New group chat — pick 2–6 bots. */}
      <GroupChatDialog
        open={groupCreateOpen}
        onOpenChange={setGroupCreateOpen}
        bots={bots}
        onCreate={handleGroupCreate}
      />

      {/* Delete confirmation — destructive, with the real consequence stated. */}
      <Dialog open={Boolean(deleteTarget)} onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('bots.delete')}</DialogTitle>
            <DialogDescription>
              {t('bots.deleteConfirm')} {deleteTarget ? `“${deleteTarget.name}”` : ''}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteTarget(null)}>{t('bots.cancel')}</Button>
            <Button variant="destructive" onClick={confirmDelete}>{t('bots.delete')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** Recency for defaulting to the most recent conversation: the last run, else
 *  the last edit. */
function recency(bot: AutomationView): number {
  const ts = bot.lastRunAt ?? bot.updatedAt;
  return ts ? Date.parse(ts) : 0;
}

function CenterEmpty({
  firstRun, loading, onCreate, onCreateGroup,
}: {
  firstRun: boolean;
  loading: boolean;
  onCreate: () => void;
  onCreateGroup: () => void;
}) {
  const t = useT();
  if (loading && firstRun) {
    return (
      <div className="glass-card flex h-full min-h-[20rem] items-center justify-center rounded-2xl px-6 text-sm text-muted-foreground">
        {t('common.loading')}
      </div>
    );
  }
  return (
    <div className="glass-card flex h-full min-h-[20rem] flex-col items-center justify-center rounded-2xl px-6 text-center">
      <div className="glass-card-subtle flex h-14 w-14 items-center justify-center rounded-2xl text-muted-foreground">
        <Ghost size={26} />
      </div>
      <h2 className="mt-4 text-base font-semibold">
        {firstRun ? t('bots.onboarding.title') : t('bots.workspace.selectPrompt')}
      </h2>
      {firstRun && (
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">
          {t('bots.onboarding.body')}
        </p>
      )}
      <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
        <Button size="lg" onClick={onCreate}>
          <Plus size={15} /> {t('bots.onboarding.createBot')}
        </Button>
        <Button size="lg" variant="outline" onClick={onCreateGroup}>
          <Users size={15} /> {t('bots.onboarding.createGroup')}
        </Button>
      </div>
      {firstRun && (
        <p className="mt-4 max-w-sm text-[11px] text-muted-foreground">
          {t('bots.onboarding.hint')}
        </p>
      )}
    </div>
  );
}
