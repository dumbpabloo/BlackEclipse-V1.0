'use client';

// Shared schedule builder used by both the creation flow and the detail editor.
// Exposes only trigger kinds the backend implements (manual, one-time, cron via
// daily/weekday/weekly presets, interval, raw cron). No browser-only scheduling:
// the persisted trigger drives the durable backend scheduler.

import { useMemo } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import type { TranslationKey } from '@/lib/i18n';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  MIN_INTERVAL_SECONDS, intervalSecondsOf, localTimezone, type BotDraft, type ScheduleChoice,
} from '@/lib/bots/state';

const WEEKDAY_KEYS: TranslationKey[] = [
  'bots.weekday.0', 'bots.weekday.1', 'bots.weekday.2',
  'bots.weekday.3', 'bots.weekday.4', 'bots.weekday.5', 'bots.weekday.6',
];

export interface ScheduleEditorProps {
  draft: BotDraft;
  onChange: (patch: Partial<BotDraft>) => void;
  className?: string;
}

export function ScheduleEditor({ draft, onChange, className }: ScheduleEditorProps) {
  const t = useT();
  const choices: ScheduleChoice[] = [
    'manual', 'once', 'daily', 'weekdays', 'weekly', 'monthly', 'interval', 'cron',
    'event', 'condition', 'webhook',
  ];
  // The first filter field drives the two-input editor; the practical case is
  // "wake when this bot finishes" (`automationId`). Extra keys authored through
  // the API are preserved verbatim on save.
  const filterEntry = Object.entries(draft.eventFilter)[0] ?? ['', ''];
  // Every IANA zone the engine accepts. `supportedValuesOf` is widely available;
  // the current draft's zone is always included so the select can never show an
  // empty value.
  const zones = useMemo(() => {
    let list: string[] = [];
    try {
      const v = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone');
      if (Array.isArray(v) && v.length) list = v;
    } catch {
      /* older engine — fall back below */
    }
    if (list.length === 0) list = [localTimezone(), 'UTC'];
    if (draft.timezone && !list.includes(draft.timezone)) list = [draft.timezone, ...list];
    return list;
  }, [draft.timezone]);
  const clampedInterval = draft.schedule === 'interval' && intervalSecondsOf(draft) < MIN_INTERVAL_SECONDS;
  return (
    <div className={cn('space-y-3', className)}>
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={draft.schedule}
          onValueChange={(v) => onChange({ schedule: (v ?? draft.schedule) as ScheduleChoice })}
        >
          <SelectTrigger className="w-[13rem]" aria-label={t('bots.schedule')}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {choices.map((c) => (
              <SelectItem key={c} value={c}>
                {t(`bots.sched.${c}` as TranslationKey)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {(draft.schedule === 'daily' || draft.schedule === 'weekdays' || draft.schedule === 'weekly' || draft.schedule === 'monthly') && (
          <>
            {draft.schedule === 'weekly' && (
              <Select
                value={String(draft.weekday)}
                onValueChange={(v) => onChange({ weekday: Number(v ?? draft.weekday) })}
              >
                <SelectTrigger className="w-[9rem]" aria-label={t('bots.sched.onDay')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WEEKDAY_KEYS.map((key, i) => (
                    <SelectItem key={key} value={String(i)}>{t(key)}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {draft.schedule === 'monthly' && (
              <>
                <span className="text-sm text-muted-foreground">{t('bots.sched.onDay')}</span>
                <Input
                  type="number"
                  min={1}
                  max={28}
                  value={draft.dom}
                  onChange={(e) => onChange({ dom: Math.min(28, Math.max(1, Number(e.target.value) || 1)) })}
                  className="w-20"
                  aria-label={t('bots.sched.dayOfMonth')}
                />
              </>
            )}
            <span className="text-sm text-muted-foreground">{t('bots.sched.at')}</span>
            <Input
              type="time"
              value={draft.time}
              onChange={(e) => onChange({ time: e.target.value })}
              className="w-auto"
            />
          </>
        )}

        {draft.schedule === 'once' && (
          <Input
            type="datetime-local"
            value={draft.runAt}
            onChange={(e) => onChange({ runAt: e.target.value })}
            className="w-auto"
            aria-label={t('bots.runAt')}
          />
        )}

        {draft.schedule === 'interval' && (
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">{t('bots.every')}</span>
            <Input
              type="number"
              min={1}
              value={draft.intervalValue}
              onChange={(e) => onChange({ intervalValue: Math.max(1, Number(e.target.value) || 1) })}
              className="w-20"
            />
            <Select
              value={draft.intervalUnit}
              onValueChange={(v) => onChange({ intervalUnit: (v ?? draft.intervalUnit) as BotDraft['intervalUnit'] })}
            >
              <SelectTrigger className="w-[7rem]" aria-label={t('bots.every')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="seconds">{t('bots.seconds')}</SelectItem>
                <SelectItem value="minutes">{t('bots.minutes')}</SelectItem>
                <SelectItem value="hours">{t('bots.hours')}</SelectItem>
                <SelectItem value="days">{t('bots.days')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        )}
        {clampedInterval && (
          <p className="text-xs text-amber-400/90">{t('bots.sched.minInterval')}</p>
        )}

        {draft.schedule === 'cron' && (
          <Input
            value={draft.cron}
            onChange={(e) => onChange({ cron: e.target.value })}
            placeholder="0 8 * * 1-5"
            className="min-w-[12rem] flex-1 font-mono"
            aria-label={t('bots.cronExpression')}
          />
        )}
      </div>
      {draft.schedule === 'event' && (
        <div className="space-y-2 rounded-xl border border-border/15 bg-white/[0.02] p-2">
          <Input
            value={draft.eventType ?? ''}
            onChange={(e) => onChange({ eventType: e.target.value })}
            placeholder="automation.completed"
            className="font-mono"
            aria-label="Event type"
          />
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={filterEntry[0]}
              onChange={(e) =>
                onChange({
                  eventFilter: e.target.value.trim()
                    ? { [e.target.value.trim()]: String(filterEntry[1] ?? '') }
                    : {},
                })
              }
              placeholder="automationId"
              className="w-[11rem] font-mono"
              aria-label="Event filter field"
            />
            <span className="text-xs text-muted-foreground">=</span>
            <Input
              value={String(filterEntry[1] ?? '')}
              onChange={(e) =>
                onChange({
                  eventFilter: filterEntry[0]
                    ? { [String(filterEntry[0])]: e.target.value }
                    : {},
                })
              }
              placeholder="the bot id to wake on"
              className="min-w-[12rem] flex-1 font-mono"
              aria-label="Event filter value"
            />
          </div>
          <p className="text-[11px] text-muted-foreground">
            A dotted event name. Every filter field must match; leave both empty to accept the event from anywhere.
          </p>
        </div>
      )}
      {draft.schedule === 'condition' && (
        <div className="space-y-2">
          <Input
            value={draft.condition?.expression ?? ''}
            onChange={(e) =>
              onChange({
                condition: {
                  expression: e.target.value,
                  // Match the backend default: a condition fires on the false→true
                  // edge, not on every tick while it remains true.
                  edge: draft.condition?.edge ?? true,
                  variables: draft.condition?.variables ?? [],
                },
              })
            }
            placeholder="AAPL.price > 200"
            className="font-mono"
            aria-label="Condition expression"
          />
          <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={draft.condition?.edge ?? true}
              onChange={(e) =>
                onChange({
                  condition: {
                    expression: draft.condition?.expression ?? '',
                    edge: e.target.checked,
                    variables: draft.condition?.variables ?? [],
                  },
                })
              }
              className="h-3.5 w-3.5 accent-[var(--primary)]"
            />
            {t('bots.conditionEdge')}
          </label>
        </div>
      )}
      {draft.schedule !== 'manual' && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{t('bots.timezone')}</span>
          <Select value={draft.timezone} onValueChange={(v) => onChange({ timezone: v ?? draft.timezone })}>
            <SelectTrigger className="w-auto min-w-[12rem]" aria-label={t('bots.timezone')}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="max-h-72 overflow-y-auto">
              {zones.map((z) => (
                <SelectItem key={z} value={z}>{z}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  );
}
