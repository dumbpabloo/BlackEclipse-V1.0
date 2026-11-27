'use client';

// ── New bot dialog ───────────────────────────────────────────────────────────
// The primary creation surface: pick a face (or upload a photo), name it, give it
// a title and a description, and optionally open Advanced to fine-tune the
// schedule. Nothing is saved until "Create bot" — creation persists through the
// REAL automation API and the chosen avatar through the client avatar store.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import { ChevronDown, Briefcase, ImagePlus, Landmark, Lock, Shuffle, Sparkles, TrendingUp } from '@/lib/icons';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { GhostAvatar } from '@/components/bots/ghost-avatar';
import { ScheduleEditor } from '@/components/bots/schedule-editor';
import { RunBehaviourEditor } from '@/components/bots/run-behaviour-editor';
import { BotModelPicker } from '@/components/bots/bot-model-picker';
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { createAutomation, prepareBotTrading, updateAutomation, type AutomationView } from '@/lib/harness/clients/automation-browser';
import { listWorkflows, type WorkflowSummary } from '@/lib/graph-studio/persistence-client';
import { TradingSetupPanel } from '@/components/bots/trading/trading-setup-panel';
import {
  buildTradingAction, buildTradingAuthority, parseSymbols, validateTradingDraft,
  type TradingConsent, type TradingContext,
} from '@/lib/bots/trading';
import { useBotAvatarStore } from '@/lib/bots/avatar-store';
import { fileToAvatarDataUrl } from '@/lib/bots/image';
import { useAgentChoices } from '@/lib/agent-presets/use-agent-choices';
import {
  DEFAULT_GHOST_SPEC, createRng, ghostSpecFromId, ghostSpecKey, randomGhostSpec, type GhostSpec,
} from '@/lib/bots/ghost';
import {
  buildModelConfig, buildPolicies, buildTrigger, detectSchedule, draftFromBot, emptyBotDraft,
  validateDraft, type BotDraft, type DraftIssue,
} from '@/lib/bots/state';

const FACE_COUNT = 12;

type Tab = 'bot' | 'generate' | 'upload' | 'pet';

const TABS: ReadonlyArray<{ id: Tab; labelKey: string }> = [
  { id: 'bot', labelKey: 'bots.dialog.tab.bot' },
  { id: 'generate', labelKey: 'bots.dialog.tab.generate' },
  { id: 'upload', labelKey: 'bots.dialog.tab.upload' },
  { id: 'pet', labelKey: 'bots.dialog.tab.pet' },
];

const PET_VARIANTS = [2, 3, 1, 2, 3, 0, 2, 1, 3, 0, 1, 2] as const;

export interface CreateBotDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existingNames: string[];
  /** The user's bots — a new bot can start from one of them. */
  bots?: AutomationView[];
  onCreated: (bot: AutomationView) => void;
  /** Optional ready-made starters. Kept here so the empty page has two actions. */
  onUseTemplate?: (template: 'desk' | 'institution' | 'finance' | 'eur1m') => void;
}

const TEMPLATES = [
  { id: 'finance', icon: Sparkles, titleKey: 'bots.templates.finance', bodyKey: 'bots.templates.financeBody' },
  { id: 'eur1m', icon: TrendingUp, titleKey: 'bots.templates.eur1m', bodyKey: 'bots.templates.eur1mBody' },
  { id: 'institution', icon: Landmark, titleKey: 'bots.templates.institution', bodyKey: 'bots.templates.institutionBody' },
  { id: 'desk', icon: Briefcase, titleKey: 'bots.templates.desk', bodyKey: 'bots.templates.deskBody' },
] as const;

export function CreateBotDialog({
  open, onOpenChange, existingNames, bots = [], onCreated, onUseTemplate,
}: CreateBotDialogProps) {
  const t = useT();
  const setAvatar = useBotAvatarStore((s) => s.setAvatar);
  const setAvatarImage = useBotAvatarStore((s) => s.setImage);

  const [tab, setTab] = useState<Tab>('bot');
  const [seed, setSeed] = useState(() => Math.floor(Math.random() * 1e9));
  const [variantLock, setVariantLock] = useState<number | null>(null);
  const [spec, setSpec] = useState<GhostSpec>(() => randomGhostSpec(createRng()));
  const [locked, setLocked] = useState(false);
  const [followName, setFollowName] = useState(false);
  const [image, setImage] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [draft, setDraft] = useState<BotDraft>(() => emptyBotDraft());
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [tradingConsent, setTradingConsent] = useState<TradingConsent | null>(null);
  const [tradingContext, setTradingContext] = useState<TradingContext>({
    strategies: [],
    connections: [],
  });
  // True once the operator edits the schedule by hand in Advanced — after that,
  // a schedule phrase in the description must NOT override their explicit choice.
  const [scheduleTouched, setScheduleTouched] = useState(false);
  const [detectedSchedule, setDetectedSchedule] = useState<string | null>(null);
  // True while the current schedule came from the description (not the operator).
  // Lets us revert to "on demand" when that phrase is deleted, instead of leaving
  // a phantom schedule the user can no longer see.
  const [autoSchedule, setAutoSchedule] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [issues, setIssues] = useState<DraftIssue[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The bot this draft was started from, or ''. Never a live link — a copy. */
  const [reuseId, setReuseId] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  // The live agent list: built-ins + the operator's own presets. A saved value
  // the runtime no longer knows is shown (labelled) instead of vanishing, so an
  // edit cannot silently retarget the bot to the default agent.
  const {
    choices: agentChoices,
    loading: agentChoicesLoading,
    error: agentChoicesError,
  } = useAgentChoices(open, draft.agentId);

  const agentGroups = useMemo(() => ({
    builtins: agentChoices.filter((choice) => choice.source === 'builtin'),
    presets: agentChoices.filter((choice) => choice.source === 'preset'),
    unknown: agentChoices.filter((choice) => choice.source === 'unknown'),
  }), [agentChoices]);

  // Fresh dialog each time it opens — a new face, empty fields, collapsed Advanced.
  useEffect(() => {
    if (!open) return;
    const nextSeed = Math.floor(Math.random() * 1e9);
    setTab('bot');
    setSeed(nextSeed);
    setVariantLock(null);
    setSpec(randomGhostSpec(createRng(nextSeed)));
    setLocked(false);
    setFollowName(false);
    setImage(null);
    setName('');
    setTitle('');
    setDescription('');
    setDraft(emptyBotDraft());
    setScheduleTouched(false);
    setDetectedSchedule(null);
    setAutoSchedule(false);
    setAdvanced(false);
    setIssues([]);
    setError(null);
    setReuseId('');
    void listWorkflows()
      .then(setWorkflows)
      .catch(() => setWorkflows([]));
  }, [open]);

  const candidates = useMemo<GhostSpec[]>(() => {
    const rng = createRng(seed);
    return Array.from({ length: FACE_COUNT }, () => {
      const face = randomGhostSpec(rng);
      return variantLock === null ? face : { ...face, variant: variantLock };
    });
  }, [seed, variantLock]);

  const petFaces = useMemo<GhostSpec[]>(() => {
    const rng = createRng(seed + 7);
    return Array.from({ length: FACE_COUNT }, (_, i) => ({
      ...randomGhostSpec(rng),
      variant: PET_VARIANTS[i % PET_VARIANTS.length],
    }));
  }, [seed]);

  const nameKey = useMemo(() => name.trim().toLowerCase(), [name]);
  const issueFor = (field: DraftIssue['field']) => issues.find((i) => i.field === field);

  const randomize = useCallback(() => {
    if (locked) return;
    const nextSeed = Math.floor(Math.random() * 1e9);
    setSeed(nextSeed);
    setFollowName(false);
    setSpec(randomGhostSpec(createRng(nextSeed)));
  }, [locked]);

  const onNameChange = (value: string) => {
    setName(value);
    if (followName) setSpec(ghostSpecFromId(value.trim() || 'new-bot'));
  };

  // Read a schedule out of the description ("check my portfolio every 5 minutes")
  // and apply it, unless the operator has already set the schedule by hand.
  const onDescriptionChange = (value: string) => {
    setDescription(value);
    setIssues((prev) => prev.filter((i) => i.field !== 'objective'));
    if (scheduleTouched) return;
    const detected = detectSchedule(value);
    if (detected) {
      setDraft((d) => ({ ...d, ...detected.patch }));
      setDetectedSchedule(detected.summary);
      setAutoSchedule(true);
    } else {
      setDetectedSchedule(null);
      // The phrase that scheduled it is gone — undo the auto-schedule so the bot
      // is not silently left on a cadence the description no longer mentions.
      if (autoSchedule) {
        setDraft((d) => ({ ...d, schedule: 'manual' }));
        setAutoSchedule(false);
      }
    }
  };

  const pickFace = (face: GhostSpec) => {
    setFollowName(false);
    setSpec(face);
  };

  const useClassicShapes = () => {
    setFollowName(false);
    setVariantLock(0);
    setSpec({ ...DEFAULT_GHOST_SPEC, bodyColor: spec.bodyColor, accentColor: spec.accentColor });
  };

  const reuseSource = useMemo(
    () => (reuseId ? bots.find((bot) => bot.id === reuseId) ?? null : null),
    [bots, reuseId],
  );

  // Start from an existing bot: a COPY of its brain, schedule and identity.
  // Nothing links the two afterwards; editing this draft never touches the
  // original, and the original keeps its own schedule.
  const applyReuse = (id: string) => {
    setReuseId(id);
    const source = bots.find((bot) => bot.id === id);
    if (!source) return;
    const base = draftFromBot(source);
    const taken = new Set(existingNames.map((name) => name.trim().toLowerCase()));
    let candidate = `Copy of ${source.name}`.slice(0, 160);
    let suffix = 2;
    while (taken.has(candidate.trim().toLowerCase())) {
      candidate = `Copy of ${source.name} (${suffix})`.slice(0, 160);
      suffix += 1;
    }
    setName(candidate);
    setTitle(source.description ?? '');
    setDescription(source.action.objective ?? source.description ?? '');
    setDraft({
      ...base,
      name: candidate,
      // A copy never inherits another bot's market authority: the trial and the
      // consent are per-bot and must be granted again.
      trading: { ...base.trading, trialId: '', consent: null, consentPhrase: '' },
    });
    setTradingConsent(null);
    setIssues([]);
    // The objective text must not re-detect a schedule over the copied one.
    setScheduleTouched(true);
    setDetectedSchedule(null);
    setAutoSchedule(false);
    // Keep the face, so the copy is recognisable as a sibling.
    const avatarState = useBotAvatarStore.getState();
    const face = avatarState.avatars[id];
    if (face) setSpec(face);
    setImage(avatarState.images[id] ?? null);
    setFollowName(false);
  };

  const onUpload = async (file: File | undefined) => {
    if (!file) return;
    try {
      setError(null);
      setImage(await fileToAvatarDataUrl(file));
      setTab('upload');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not read the image.');
    }
  };

  const tradingCheck = useMemo(
    () =>
      validateTradingDraft(draft.trading, tradingContext, tradingConsent, {
        requireStrategy: draft.kind === 'strategy',
      }),
    [draft.trading, draft.kind, tradingContext, tradingConsent],
  );

  const create = useCallback(async () => {
    if (saving) return;
    const nextDraft: BotDraft = { ...draft, name: name.trim(), objective: description.trim() };
    // Only enforce the agent id when the live list actually loaded; a network
    // failure must not block saving an unrelated edit.
    const knownAgentIds = agentChoicesError
      ? undefined
      : agentChoices.filter((choice) => choice.source !== 'unknown').map((choice) => choice.id);
    const found = validateDraft(nextDraft, knownAgentIds);
    if (nameKey && existingNames.some((n) => n.trim().toLowerCase() === nameKey)) {
      found.push({ field: 'name', message: t('bots.create.duplicate') });
    }
    const isStrategy = nextDraft.kind === 'strategy';
    const agentTrading =
      nextDraft.kind === 'agent' &&
      nextDraft.trading.enabled &&
      nextDraft.trading.mode !== 'shadow';
    if (isStrategy || agentTrading) {
      for (const issue of tradingCheck.issues) {
        if (issue.severity === 'error') found.push({ field: 'trading', message: issue.message });
      }
    }
    setIssues(found);
    if (found.length) return;
    // A reused bot that runs on a cadence starts PAUSED: two copies firing at
    // the same minute is a surprise, and the user opted into creating a bot,
    // not into duplicating its schedule.
    const cloneScheduled = Boolean(reuseSource) && buildTrigger(nextDraft).type !== 'manual';
    // A live choice is the operator's grant: the server still checks the typed
    // phrase and an armed connection, but the tier must match the mode we send.
    const authority = buildTradingAuthority(nextDraft.trading);
    if (nextDraft.trading.mode === 'live') authority.tier = 'l2';
    setSaving(true);
    setError(null);
    try {
      let bot = await createAutomation({
        name: nextDraft.name,
        description: title.trim() || undefined,
        // A strategy bot is created as a DRAFT: trading is prepared (trial +
        // mandate + consent) before it is allowed to run. A failure leaves a
        // visible draft instead of a bot that silently never trades.
        state: isStrategy || agentTrading ? 'draft' : cloneScheduled ? 'draft' : 'active',
        trigger: buildTrigger(nextDraft),
        action: isStrategy
          ? {
              type: 'run_strategy' as const,
              objective: nextDraft.objective,
              config: buildTradingAction(nextDraft.trading, nextDraft.objective, tradingConsent)
                .config as unknown as Record<string, unknown>,
              workflowId: null,
              agentId: null,
              workflowInput: {},
              toolName: null,
              toolArgs: {},
              taskTitle: null,
              skillId: null,
              skillVersion: null,
              workspaceRoot: null,
              autonomy: null,
              notificationTemplate: null,
              credentialRef: null,
            }
          : nextDraft.kind === 'workflow'
            ? {
                type: 'execute_workflow',
                objective: nextDraft.objective,
                workflowId: null,
                workflowInput: {},
                config: {
                  graphWorkflowId: nextDraft.workflowId,
                  graphWorkflowName: workflows.find((w) => w.id === nextDraft.workflowId)?.name ?? null,
                },
              }
            : {
                type: 'run_agent', objective: nextDraft.objective, agentId: nextDraft.agentId,
                config: {
                  ...buildModelConfig(nextDraft),
                  // A trading agent carries its mode and reference images into
                  // every chat/scheduled turn (the persona reads them).
                  ...(nextDraft.trading.enabled
                    ? {
                        mode: nextDraft.trading.mode,
                        visuals: nextDraft.trading.visuals,
                      }
                    : {}),
                },
              },
        executionAuthority: authority,
        ...buildPolicies(nextDraft),
        tags: isStrategy ? ['bot', 'trading'] : ['bot'],
      });

      if (isStrategy || agentTrading) {
        const trading = nextDraft.trading;
        try {
          const prepared = await prepareBotTrading(bot.id, {
            strategyId: isStrategy ? trading.strategyId : "",
            strategyVersion: isStrategy ? trading.strategyVersion : "",
            mode: trading.mode,
            connectionId: trading.connectionId,
            symbols: parseSymbols(trading.symbolsText),
            timeframe: trading.timeframe,
            volume: trading.volume,
            stopLossPoints: trading.stopLossPoints,
            takeProfitPoints: trading.takeProfitPoints,
            maxOrders: trading.maxOrdersPerDay,
            maxMargin: trading.maxMarginPerOrder,
            confirmation: trading.mode === 'shadow' ? '' : trading.consentPhrase,
          });
          bot = prepared.automation;
          if (!cloneScheduled) {
            bot = await updateAutomation(bot.id, { state: 'active' });
          }
        } catch (err) {
          // The bot exists as a draft; say exactly that. No half-prepared bot is
          // ever activated, and the operator can retry from its panel.
          setAvatar(bot.id, spec);
          setAvatarImage(bot.id, image);
          onCreated(bot);
          setError(
            `Bot created as a draft, but trading could not be prepared: ${
              err instanceof Error ? err.message : 'unknown error'
            }. Open it and retry.`,
          );
          return;
        }
      }

      setAvatar(bot.id, spec);
      setAvatarImage(bot.id, image);
      onCreated(bot);
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'creation failed');
    } finally {
      setSaving(false);
    }
  }, [saving, draft, name, description, title, nameKey, existingNames, t, spec, image, workflows, onCreated, onOpenChange, setAvatar, setAvatarImage, agentChoices, agentChoicesError, reuseSource, tradingCheck, tradingConsent]);

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!saving) onOpenChange(next); }}>
      <DialogContent className="glass-card-strong max-h-[92vh] overflow-y-auto rounded-2xl border border-white/15 dark:border-white/10 sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t('bots.dialog.title')}</DialogTitle>
          <DialogDescription>{t('bots.dialog.subtitle')}</DialogDescription>
        </DialogHeader>

        {/* Ready-made starters. They replace the six buttons that used to crowd
            the empty Bots page; the page now offers only Create bot / Group chat. */}
        {onUseTemplate && (
          <div>
            <div className="mb-1.5 text-[11px] font-medium text-muted-foreground">
              {t('bots.templates.title')}
            </div>
            <div className="grid gap-1.5 sm:grid-cols-2">
              {TEMPLATES.map(({ id, icon: Icon, titleKey, bodyKey }) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => onUseTemplate(id)}
                  className="group flex items-start gap-2 rounded-xl border border-white/10 bg-white/[0.02] px-2.5 py-2 text-left transition-colors hover:border-white/20 hover:bg-white/[0.05]"
                >
                  <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white/[0.06] text-primary">
                    <Icon size={14} />
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-[12px] font-semibold">{t(titleKey)}</span>
                    <span className="block text-[10px] leading-snug text-muted-foreground">{t(bodyKey)}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Start from an existing bot (a copy — the original is untouched). */}
        {bots.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-muted-foreground">Start from</div>
            <select
              value={reuseId}
              onChange={(event) => {
                if (event.target.value) applyReuse(event.target.value);
                else setReuseId('');
              }}
              className="w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring"
            >
              <option value="">Blank bot</option>
              {bots.map((bot) => (
                <option key={bot.id} value={bot.id}>
                  {bot.name}
                  {bot.action.type === 'execute_workflow' ? ' · workflow' : ' · agent'}
                </option>
              ))}
            </select>
            {reuseSource && (
              <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
                Copies its brain, objective{reuseSource.trigger.type !== 'manual' ? ', schedule (created paused)' : ''} and face.
                The original keeps its own schedule; editing this copy never changes it.
              </p>
            )}
          </div>
        )}

        {/* Face preview */}
        <div className="flex justify-center">
          <div className="glass-card-subtle flex h-20 w-20 items-center justify-center rounded-2xl">
            {image ? (
              <img src={image} alt="" className="h-16 w-16 rounded-xl object-cover" />
            ) : (
              <GhostAvatar spec={spec} size={62} />
            )}
          </div>
        </div>

        {/* Tab strip */}
        <div role="tablist" className="grid grid-cols-4 gap-1 rounded-xl glass-card-subtle p-1">
          {TABS.map(({ id, labelKey }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              onClick={() => setTab(id)}
              className={cn(
                'rounded-lg py-1.5 text-xs font-semibold transition-colors cursor-pointer',
                tab === id ? 'bg-white/[0.09] text-foreground' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {t(labelKey as never)}
            </button>
          ))}
        </div>

        {tab === 'upload' ? (
          <div className="space-y-3">
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="glass-card-subtle flex w-full flex-col items-center gap-1.5 rounded-xl border border-dashed border-border/50 px-4 py-6 text-xs text-muted-foreground hover:border-border hover:text-foreground"
            >
              <ImagePlus size={20} />
              {t('bots.dialog.uploadHint')}
              <span className="font-semibold text-foreground">{t('bots.dialog.chooseFile')}</span>
            </button>
            {image && (
              <div className="flex items-center justify-between gap-2 rounded-xl glass-card-subtle px-3 py-2">
                <span className="flex items-center gap-2 text-xs">
                  <img src={image} alt="" className="h-7 w-7 rounded-md object-cover" />
                  {t('bots.dialog.uploaded')}
                </span>
                <button type="button" onClick={() => setImage(null)} className="text-xs text-muted-foreground hover:text-foreground">
                  {t('bots.dialog.removeImage')}
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2.5">
            <div className="grid grid-cols-6 gap-1.5">
              {tab === 'bot' && (
                <button
                  type="button"
                  onClick={randomize}
                  disabled={locked}
                  title={t('bots.dialog.auto')}
                  className="flex h-11 items-center justify-center rounded-xl border border-transparent bg-white/[0.03] text-[10px] font-semibold text-muted-foreground hover:bg-white/[0.07] disabled:opacity-40"
                >
                  {t('bots.dialog.auto')}
                </button>
              )}
              {(tab === 'pet' ? petFaces : tab === 'generate' ? candidates.slice(0, 1) : candidates).map((face, i) => {
                const active = !image && ghostSpecKey(face) === ghostSpecKey(spec);
                return (
                  <button
                    key={`${ghostSpecKey(face)}-${i}`}
                    type="button"
                    onClick={() => pickFace(face)}
                    aria-pressed={active}
                    className={cn(
                      'flex h-11 items-center justify-center rounded-xl border transition-colors cursor-pointer',
                      active ? 'border-white/40 bg-white/[0.10]' : 'border-transparent bg-white/[0.03] hover:bg-white/[0.07]',
                    )}
                  >
                    <GhostAvatar spec={face} size={30} />
                  </button>
                );
              })}
            </div>

            <div className="flex items-center justify-center gap-2 pt-0.5">
              <button
                type="button"
                onClick={randomize}
                disabled={locked}
                className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-muted-foreground hover:bg-white/[0.06] hover:text-foreground disabled:opacity-40 cursor-pointer"
              >
                <Shuffle size={13} /> {tab === 'generate' ? t('bots.dialog.generate') : t('bots.dialog.randomize')}
              </button>
              <button
                type="button"
                onClick={() => setLocked((v) => !v)}
                aria-pressed={locked}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium cursor-pointer',
                  locked ? 'bg-white/[0.09] text-foreground' : 'text-muted-foreground hover:bg-white/[0.06] hover:text-foreground',
                )}
              >
                <Lock size={13} /> {locked ? t('bots.dialog.unlockFace') : t('bots.dialog.lockFace')}
              </button>
            </div>

            {tab === 'bot' && (
              <div className="flex items-center justify-center gap-4 text-[11px] text-muted-foreground">
                <button
                  type="button"
                  onClick={() => { setFollowName(true); setSpec(ghostSpecFromId(name.trim() || 'new-bot')); setVariantLock(null); }}
                  className={cn('hover:text-foreground cursor-pointer', followName && 'text-foreground')}
                >
                  {t('bots.dialog.followName')}
                </button>
                <button type="button" onClick={useClassicShapes} className="hover:text-foreground cursor-pointer">
                  {t('bots.dialog.classicShapes')}
                </button>
              </div>
            )}
          </div>
        )}

        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => { void onUpload(e.target.files?.[0]); e.target.value = ''; }}
        />

        {/* Identity fields */}
        <div className="space-y-3">
          <Field label={`${t('bots.dialog.name')} *`} htmlFor="new-bot-name">
            <Input
              id="new-bot-name"
              value={name}
              onChange={(e) => onNameChange(e.target.value)}
              placeholder={t('bots.dialog.namePlaceholder')}
              autoFocus
            />
            <FieldError issue={issueFor('name')} />
          </Field>
          <Field label={t('bots.dialog.titleField')} htmlFor="new-bot-title">
            <Input
              id="new-bot-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={t('bots.dialog.titlePlaceholder')}
            />
          </Field>
          <Field label={`${t('bots.dialog.description')} *`} htmlFor="new-bot-description">
            <textarea
              id="new-bot-description"
              value={description}
              onChange={(e) => onDescriptionChange(e.target.value)}
              rows={3}
              placeholder={t('bots.dialog.descriptionPlaceholder')}
              className="min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
            />
            <FieldError issue={issueFor('objective')} />
            {detectedSchedule && !scheduleTouched && (
              <p className="mt-1 flex items-center gap-1.5 text-[11px] text-emerald-300/90">
                <Sparkles size={11} className="shrink-0" />
                {t('bots.schedule')}: {detectedSchedule}
              </p>
            )}
          </Field>
        </div>

        {/* Brain — the existing agent loop, a saved workflow graph, or a strategy. */}
        <div>
          <div className="mb-1 text-[11px] font-medium text-muted-foreground">Brain</div>
          <div className="grid grid-cols-3 gap-1 rounded-xl glass-card-subtle p-1">
            <button
              type="button"
              onClick={() => setDraft((d) => ({ ...d, kind: 'agent' }))}
              className={cn(
                'rounded-lg py-1.5 text-xs font-semibold transition-colors cursor-pointer',
                draft.kind === 'agent' ? 'bg-white/[0.09] text-foreground' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              Agent
            </button>
            <button
              type="button"
              onClick={() => setDraft((d) => ({ ...d, kind: 'workflow' }))}
              className={cn(
                'rounded-lg py-1.5 text-xs font-semibold transition-colors cursor-pointer',
                draft.kind === 'workflow' ? 'bg-white/[0.09] text-foreground' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              Workflow
            </button>
            <button
              type="button"
              onClick={() =>
                setDraft((d) => ({
                  ...d,
                  kind: 'strategy',
                  trading: { ...d.trading, enabled: true },
                }))
              }
              className={cn(
                'rounded-lg py-1.5 text-xs font-semibold transition-colors cursor-pointer',
                draft.kind === 'strategy' ? 'bg-white/[0.09] text-foreground' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              Strategy
            </button>
          </div>
          {draft.kind === 'workflow' && (
            <div className="mt-2 space-y-1.5">
              <select
                value={draft.workflowId}
                onChange={(event) => {
                  setDraft((d) => ({ ...d, workflowId: event.target.value }));
                  setIssues((prev) => prev.filter((issue) => issue.field !== 'workflowId'));
                }}
                className="w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring"
              >
                <option value="">Choose a saved workflow…</option>
                {workflows.map((workflow) => (
                  <option key={workflow.id} value={workflow.id}>
                    {workflow.name} (v{workflow.currentVersion})
                  </option>
                ))}
              </select>
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                Each chat message is passed to the workflow as its trigger input. Scheduled
                runs use the objective above as the input. Workflow bots answer from the
                graph&apos;s terminal output.
              </p>
              <FieldError issue={issueFor('workflowId')} />
            </div>
          )}
        </div>

        {draft.kind === 'strategy' && (
          <TradingSetupPanel
            draft={draft.trading}
            consent={tradingConsent}
            issues={tradingCheck.issues}
            disabled={saving}
            onChange={(patch) =>
              setDraft((d) => ({
                ...d,
                trading: { ...d.trading, ...patch, enabled: true },
              }))
            }
            onConsent={setTradingConsent}
            onContext={setTradingContext}
          />
        )}

        {/* Agent brain — preset + model, visible without opening Advanced. */}
        {draft.kind === 'agent' && (
          <div className="space-y-2 rounded-xl border border-border/15 p-3">
            <div>
              <div className="mb-1 text-[11px] font-medium text-muted-foreground">{t('bots.create.agent')}</div>
              <Select
                value={draft.agentId}
                onValueChange={(v) => {
                  setDraft((d) => ({ ...d, agentId: v ?? d.agentId }));
                  setIssues((prev) => prev.filter((i) => i.field !== 'agentId'));
                }}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {agentGroups.builtins.length > 0 && (
                    <SelectGroup>
                      <SelectLabel>Built-in</SelectLabel>
                      {agentGroups.builtins.map((choice) => (
                        <SelectItem key={choice.id} value={choice.id}>{choice.label}</SelectItem>
                      ))}
                    </SelectGroup>
                  )}
                  {agentGroups.presets.length > 0 && (
                    <SelectGroup>
                      <SelectLabel>Your presets</SelectLabel>
                      {agentGroups.presets.map((choice) => (
                        <SelectItem key={choice.id} value={choice.id}>
                          {choice.label}
                          {choice.archived ? ' (archived)' : ''}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  )}
                  {agentGroups.unknown.length > 0 && (
                    <SelectGroup>
                      <SelectLabel>Not found</SelectLabel>
                      {agentGroups.unknown.map((choice) => (
                        <SelectItem key={choice.id} value={choice.id}>{choice.label}</SelectItem>
                      ))}
                    </SelectGroup>
                  )}
                </SelectContent>
              </Select>
              {agentChoicesLoading && (
                <p className="mt-1 text-[10px] text-muted-foreground">Loading agents…</p>
              )}
              {agentChoicesError && (
                <p className="mt-1 text-[10px] text-amber-300">{agentChoicesError}</p>
              )}
              <FieldError issue={issueFor('agentId')} />
            </div>
            <BotModelPicker
              provider={draft.modelProvider}
              model={draft.model}
              onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
            />
          </div>
        )}

        {/* Advanced — schedule + agent, collapsed by default */}
        <div className="rounded-xl glass-card-subtle">
          <button
            type="button"
            onClick={() => setAdvanced((v) => !v)}
            aria-expanded={advanced}
            className="flex w-full items-center gap-1.5 px-3 py-2 text-xs font-semibold text-muted-foreground hover:text-foreground cursor-pointer"
          >
            <ChevronDown size={13} className={cn('transition-transform', !advanced && '-rotate-90')} />
            {t('bots.dialog.advanced')}
          </button>
          {advanced && (
            <div className="space-y-3 border-t border-border/10 px-3 py-3">
              {draft.kind === 'agent' && (
                <>
                  {/* Agent trading: the same leash as a strategy bot, but the
                      agent decides with its tools. L0 proposes, L1 demo, L2 live. */}
                  <div className="rounded-lg border border-border/15 p-2.5">
                    <label className="flex items-center justify-between gap-2 text-[11px] font-semibold">
                      Let this agent trade
                      <input
                        type="checkbox"
                        checked={draft.trading.enabled}
                        onChange={(event) =>
                          setDraft((d) => ({
                            ...d,
                            trading: {
                              ...d.trading,
                              enabled: event.target.checked,
                              mode:
                                event.target.checked && d.trading.mode === 'shadow'
                                  ? 'demo'
                                  : d.trading.mode,
                            },
                          }))
                        }
                      />
                    </label>
                    <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
                      At L0 the agent proposes orders for your approval. L1 fills a
                      demo account; L2 may fill live under a bot-pinned mandate. The
                      setup below grants that authority once.
                    </p>
                  </div>
                  {draft.trading.enabled && (
                    <TradingSetupPanel
                      draft={draft.trading}
                      consent={tradingConsent}
                      issues={tradingCheck.issues}
                      disabled={saving}
                      brain="agent"
                      onChange={(patch) =>
                        setDraft((d) => ({
                          ...d,
                          trading: { ...d.trading, ...patch, enabled: true },
                        }))
                      }
                      onConsent={setTradingConsent}
                      onContext={setTradingContext}
                    />
                  )}
                </>
              )}
              <div>
                <div className="mb-1 text-[11px] font-medium text-muted-foreground">{t('bots.dialog.schedule')}</div>
                <ScheduleEditor
                  draft={draft}
                  onChange={(patch) => {
                    setScheduleTouched(true);
                    setDetectedSchedule(null);
                    setAutoSchedule(false);
                    setDraft((d) => ({ ...d, ...patch }));
                    setIssues((prev) => prev.filter((i) => i.field !== 'schedule' && i.field !== 'cron' && i.field !== 'runAt' && i.field !== 'intervalValue'));
                  }}
                />
                <FieldError issue={issueFor('schedule') ?? issueFor('cron') ?? issueFor('runAt') ?? issueFor('intervalValue')} />
              </div>
              <div>
                <div className="mb-1 text-[11px] font-medium text-muted-foreground">{t('bots.behaviour.title')}</div>
                <RunBehaviourEditor
                  draft={draft}
                  onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
                />
              </div>
            </div>
          )}
        </div>

        {error && (
          <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-300">
            {error}
          </div>
        )}

        {(() => {
          const missing: string[] = [];
          if (!name.trim()) missing.push('a name');
          if (!description.trim()) missing.push('what it should do');
          if (draft.kind === 'workflow' && !draft.workflowId) missing.push('a workflow');
          if (!missing.length) return null;
          return (
            <p className="text-[11px] text-amber-300">
              Add {missing.join(', ')} to create the bot.
            </p>
          );
        })()}

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={saving}>
            {t('bots.dialog.cancel')}
          </Button>
          <Button
            onClick={() => void create()}
            disabled={saving || !name.trim() || !description.trim() || (draft.kind === 'workflow' && !draft.workflowId)}
          >
            {saving ? t('bots.dialog.creating') : (
              <>
                <Sparkles size={14} /> {t('bots.dialog.create')}
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="mb-1 block text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
    </div>
  );
}

function FieldError({ issue }: { issue?: DraftIssue }) {
  if (!issue) return null;
  return <p className="mt-1 text-xs text-rose-300">{issue.message}</p>;
}
