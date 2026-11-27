'use client';

// ── Group settings ────────────────────────────────────────────────────────────
// The group room's "Settings": rename it, change its photo, and manage members
// (2–6). Each member row also surfaces its real schedule + lifecycle state so the
// room's automation is visible here, not buried in the bot's own page.

import { useEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import { Check, ImagePlus, Search, Users } from '@/lib/icons';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { BotAvatar } from '@/components/bots/bot-avatar';
import { avatarFor, useBotAvatarStore, useHydrateBotAvatars } from '@/lib/bots/avatar-store';
import { fileToAvatarDataUrl } from '@/lib/bots/image';
import { useBotGroupsStore } from '@/lib/bots/groups-store';
import type { BotGroup, GroupMode } from '@/lib/bots/groups-store';
import { scheduleSummary } from '@/lib/bots/state';
import type { AutomationView } from '@/lib/harness/clients/automation-browser';

const MIN_MEMBERS = 2;
const MAX_MEMBERS = 6;

export interface GroupSettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  group: BotGroup;
  bots: AutomationView[];
}

export function GroupSettingsDialog({ open, onOpenChange, group, bots }: GroupSettingsDialogProps) {
  const t = useT();
  useHydrateBotAvatars();
  const avatars = useBotAvatarStore((s) => s.avatars);
  const renameGroup = useBotGroupsStore((s) => s.renameGroup);
  const setGroupImage = useBotGroupsStore((s) => s.setGroupImage);
  const setGroupMode = useBotGroupsStore((s) => s.setGroupMode);
  const setGroupLead = useBotGroupsStore((s) => s.setGroupLead);
  const setGroupRouting = useBotGroupsStore((s) => s.setGroupRouting);
  const setMembers = useBotGroupsStore((s) => s.setMembers);

  const [name, setName] = useState(group.name);
  const [image, setImage] = useState<string | null>(group.image ?? null);
  const [mode, setMode] = useState<GroupMode>(group.mode === 'roundtable' ? 'roundtable' : 'parallel');
  const [leadId, setLeadId] = useState<string | null>(group.leadId ?? null);
  const [routingMode, setRoutingMode] = useState<'all' | 'lead'>(group.routingMode === 'lead' ? 'lead' : 'all');
  const [selected, setSelected] = useState<string[]>(group.memberIds);
  const [query, setQuery] = useState('');
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const botsRef = useRef(bots);
  botsRef.current = bots;

  useEffect(() => {
    if (!open) return;
    setName(group.name);
    setImage(group.image ?? null);
    setMode(group.mode === 'roundtable' ? 'roundtable' : 'parallel');
    setLeadId(group.leadId ?? null);
    setRoutingMode(group.routingMode === 'lead' ? 'lead' : 'all');
    setSelected(
      botsRef.current.length > 0
        ? group.memberIds.filter((id) => botsRef.current.some((b) => b.id === id))
        : group.memberIds,
    );
    setQuery('');
    setError(null);
  }, [open, group.name, group.image, group.memberIds]);

  // Once the live bot list arrives, drop any stale member id without resetting
  // the operator's valid selections (this runs on every hydrate, so it must be
  // a narrowing filter only).
  useEffect(() => {
    if (!open || bots.length === 0) return;
    setSelected((prev) => {
      const next = prev.filter((id) => bots.some((b) => b.id === id));
      return next.length === prev.length ? prev : next;
    });
  }, [open, bots]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return bots;
    return bots.filter((b) => b.name.toLowerCase().includes(q));
  }, [bots, query]);

  const toggle = (id: string) => {
    setError(null);
    setSelected((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= MAX_MEMBERS) return prev;
      return [...prev, id];
    });
  };

  const onUpload = async (file: File | undefined) => {
    if (!file) return;
    try {
      setImage(await fileToAvatarDataUrl(file));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not read the image.');
    }
  };

  const save = () => {
    if (!name.trim()) { setError(t('bots.groups.nameRequired')); return; }
    const valid = selected.filter((id) => bots.some((b) => b.id === id));
    if (valid.length < MIN_MEMBERS) { setError(t('bots.groups.minMembers')); return; }
    renameGroup(group.id, name.trim());
    setGroupImage(group.id, image);
    setGroupMode(group.id, mode);
    setMembers(group.id, valid);
    setGroupLead(group.id, leadId && valid.includes(leadId) ? leadId : null);
    setGroupRouting(group.id, routingMode);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="glass-card-strong max-h-[92vh] overflow-y-auto rounded-2xl border border-white/15 dark:border-white/10 sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('bots.groups.settingsTitle')}</DialogTitle>
          <DialogDescription>{t('bots.groups.settingsHint')}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <Input
            value={name}
            onChange={(e) => { setName(e.target.value); setError(null); }}
            placeholder={t('bots.groups.namePlaceholder')}
            aria-label={t('bots.groups.namePlaceholder')}
          />

          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            className="glass-card-subtle inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
          >
            {image ? (
              <img src={image} alt="" className="h-4 w-4 rounded object-cover" />
            ) : (
              <ImagePlus size={14} />
            )}
            {t('bots.groups.upload')}
            {image && (
              <span
                role="button"
                tabIndex={0}
                onClick={(e) => { e.stopPropagation(); setImage(null); }}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); setImage(null); } }}
                className="ml-1 text-[10px] text-rose-300 hover:text-rose-200"
              >
                {t('bots.groups.removeImage')}
              </span>
            )}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = ''; }}
          />

          <div className="space-y-1.5">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {t('bots.groups.modeLabel')}
            </div>
            <div className="grid grid-cols-2 gap-2">
              {([
                ['parallel', t('bots.groups.modeParallel'), t('bots.groups.modeParallelHint')],
                ['roundtable', t('bots.groups.modeRoundtable'), t('bots.groups.modeRoundtableHint')],
              ] as const).map(([value, label, hint]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setMode(value)}
                  aria-pressed={mode === value}
                  className={cn(
                    'rounded-xl border px-3 py-2 text-left transition-colors',
                    mode === value
                      ? 'border-primary/50 bg-primary/10'
                      : 'border-border/30 hover:bg-white/[0.04]',
                  )}
                >
                  <span className="block text-xs font-semibold text-foreground">{label}</span>
                  <span className="mt-0.5 block text-[10px] leading-snug text-muted-foreground">{hint}</span>
                </button>
              ))}
            </div>
          </div>
        </div>

        {mode === 'roundtable' && selected.length > 0 && (
          <div className="space-y-1.5">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {t('bots.groups.leadLabel')}
            </div>
            <div className="flex flex-wrap gap-1.5">
              <button
                type="button"
                onClick={() => setLeadId(null)}
                className={cn(
                  'rounded-lg border px-2.5 py-1 text-[11px]',
                  !leadId ? 'border-primary/50 bg-primary/10 text-foreground' : 'border-border/30 text-muted-foreground',
                )}
              >
                {t('bots.groups.leadNone')}
              </button>
              {selected.map((id) => {
                const bot = bots.find((b) => b.id === id);
                if (!bot) return null;
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => setLeadId(id)}
                    className={cn(
                      'rounded-lg border px-2.5 py-1 text-[11px]',
                      leadId === id ? 'border-primary/50 bg-primary/10 text-foreground' : 'border-border/30 text-muted-foreground',
                    )}
                  >
                    {bot.name}
                  </button>
                );
              })}
            </div>
            <p className="text-[10px] text-muted-foreground">{t('bots.groups.leadHint')}</p>
            <label className="flex items-center gap-2 text-[10px] text-muted-foreground">
              <input
                type="checkbox"
                checked={routingMode === 'lead'}
                onChange={(e) => setRoutingMode(e.target.checked ? 'lead' : 'all')}
                className="h-3.5 w-3.5 accent-[var(--primary)]"
              />
              {t('bots.groups.leadRoutes')}
            </label>
          </div>
        )}

        <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          <Users size={12} /> {t('bots.groups.membersTitle')} · {selected.length}/{MAX_MEMBERS}
        </div>

        <div className="relative">
          <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('bots.groups.search')}
            className="pl-8"
            aria-label={t('bots.groups.search')}
          />
        </div>

        <ul className="max-h-64 space-y-1 overflow-y-auto pr-0.5">
          {filtered.length === 0 ? (
            <li className="px-2 py-3 text-xs text-muted-foreground">{t('bots.noMatches')}</li>
          ) : (
            filtered.map((bot) => {
              const isSelected = selected.includes(bot.id);
              const disabled = !isSelected && selected.length >= MAX_MEMBERS;
              return (
                <li key={bot.id}>
                  <button
                    type="button"
                    onClick={() => toggle(bot.id)}
                    disabled={disabled}
                    aria-pressed={isSelected}
                    className={cn(
                      'flex w-full items-center gap-2.5 rounded-xl px-2 py-1.5 text-left transition-colors',
                      isSelected ? 'bg-white/[0.08]' : 'hover:bg-white/[0.04]',
                      disabled && 'opacity-40',
                    )}
                  >
                    <BotAvatar botId={bot.id} spec={avatarFor(avatars, bot.id)} size={30} title={bot.name} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-semibold text-foreground">{bot.name}</span>
                      <span className="block truncate text-[10px] text-muted-foreground">
                        {bot.state} · {scheduleSummary(bot.trigger)}
                      </span>
                    </span>
                    <span
                      className={cn(
                        'flex h-4 w-4 shrink-0 items-center justify-center rounded border',
                        isSelected ? 'border-primary bg-primary text-primary-foreground' : 'border-border/60',
                      )}
                    >
                      {isSelected && <Check size={11} weight="bold" />}
                    </span>
                  </button>
                </li>
              );
            })
          )}
        </ul>

        {error && (
          <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-300">
            {error}
          </div>
        )}

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t('bots.groups.cancel')}</Button>
          <Button onClick={save} disabled={selected.length < MIN_MEMBERS || !name.trim()}>
            {t('bots.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
