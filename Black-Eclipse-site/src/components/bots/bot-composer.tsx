'use client';

// ── Bot composer ─────────────────────────────────────────────────────────────
// One capsule for everything you send: attachments inside the border, a growing
// textarea, and attach/send(or stop) on the bottom row. Extracted from
// `bot-chat.tsx` so that file stays under the size ratchet and the two bot
// surfaces (direct chat and group rooms) can share one shape.

import { useCallback, useEffect, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import { ArrowUp, ChevronDown, FileText, Loader2, Paperclip, Pause, X } from '@/lib/icons';
import { ApprovalCard } from '@/components/chat/approval-card';
import { ClarificationCard } from '@/components/chat/clarification-card';
import { TradeApprovalBanner } from '@/components/chat/trade-approval-banner';
import type { PendingApproval, PendingClarification } from '@/lib/agent/response-model';
import type { SessionFile } from '@/lib/chat/session-types';
import type { QueuedTurn } from '@/lib/bots/queue-store';

export interface BotComposerProps {
  botId: string;
  botName: string;
  input: string;
  onInput: (value: string) => void;
  sending: boolean;
  attachBusy: boolean;
  attachError: string | null;
  contextFiles: SessionFile[];
  onAttach: () => void;
  onRemoveAttachment: (name: string) => void;
  onSend: () => void;
  onStop: () => void;
  pendingApproval?: PendingApproval;
  pendingClarification?: PendingClarification;
  onDecideApproval: (approve: boolean, remember?: boolean) => void;
  onAnswerClarification: (value: string) => void;
  queued: QueuedTurn[];
  onQueueMove: (id: string, direction: -1 | 1) => void;
  onQueueRemove: (id: string) => void;
  onQueueClear: () => void;
  onQueueSteer: (turn: QueuedTurn) => void;
}

export function BotComposer({
  botId,
  botName,
  input,
  onInput,
  sending,
  attachBusy,
  attachError,
  contextFiles,
  onAttach,
  onRemoveAttachment,
  onSend,
  onStop,
  pendingApproval,
  pendingClarification,
  onDecideApproval,
  onAnswerClarification,
  queued,
  onQueueMove,
  onQueueRemove,
  onQueueClear,
  onQueueSteer,
}: BotComposerProps) {
  const t = useT();
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [anchor, setAnchor] = useState<HTMLDivElement | null>(null);
  const [queueOpen, setQueueOpen] = useState(false);
  const [shift, setShift] = useState(false);
  const rootRef = useCallback((el: HTMLDivElement | null) => {
    setAnchor(el);
  }, []);

  // Grow with content, then scroll — capped so the thread keeps the page.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, [input]);

  // Enter sends; the hint appears only while Shift is actually held.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const down = (event: KeyboardEvent) => { if (event.key === 'Shift') setShift(true); };
    const up = (event: KeyboardEvent) => { if (event.key === 'Shift') setShift(false); };
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    return () => {
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
    };
  }, []);

  const nextQueued = queued[0];

  return (
    <div ref={rootRef} className="border-t border-border/10 p-3">
      {/* Pending decisions sit directly above the composer that answers them. */}
      <TradeApprovalBanner automationId={botId} />
      {pendingApproval && (
        <ApprovalCard
          approval={pendingApproval}
          anchor={anchor}
          onDecide={onDecideApproval}
        />
      )}
      {pendingClarification && (
        <ClarificationCard
          clarification={pendingClarification}
          anchor={anchor}
          onAnswer={onAnswerClarification}
        />
      )}

      {/* The queue collapses to one line until you need to edit it. */}
      {queued.length > 0 && (
        <div className="mb-2 rounded-xl border border-border/40 bg-white/[0.02] px-2 py-1">
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <button
              type="button"
              onClick={() => setQueueOpen((open) => !open)}
              className="flex min-w-0 flex-1 items-center gap-1.5 rounded px-0.5 py-0.5 text-left hover:text-foreground"
              aria-expanded={queueOpen}
            >
              <ChevronDown size={11} className={cn('transition-transform', !queueOpen && '-rotate-90')} />
              <span className="font-medium">{t('bots.queue.title')} · {queued.length}</span>
              <span className="min-w-0 truncate opacity-70">{nextQueued?.text}</span>
            </button>
            <button
              type="button"
              onClick={() => onQueueSteer(nextQueued)}
              className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium text-primary hover:bg-primary/10"
              title={t('bots.queue.steerHint')}
            >{t('bots.queue.steer')}</button>
          </div>
          {queueOpen && (
            <div className="mt-1 space-y-0.5 border-t border-border/20 pt-1">
              {queued.map((turn, index) => (
                <div key={turn.id} className="flex items-center gap-1.5 py-px text-[11px]">
                  <span className="min-w-0 flex-1 truncate text-muted-foreground" title={turn.text}>
                    {turn.text}
                  </span>
                  <button
                    type="button"
                    disabled={index === 0}
                    onClick={() => onQueueMove(turn.id, -1)}
                    className="rounded p-0.5 hover:bg-white/10 disabled:opacity-30"
                    title={t('bots.queue.up')}
                    aria-label={t('bots.queue.up')}
                  ><ArrowUp size={11} /></button>
                  <button
                    type="button"
                    disabled={index === queued.length - 1}
                    onClick={() => onQueueMove(turn.id, 1)}
                    className="rounded p-0.5 hover:bg-white/10 disabled:opacity-30"
                    title={t('bots.queue.down')}
                    aria-label={t('bots.queue.down')}
                  ><ArrowUp size={11} className="rotate-180" /></button>
                  <button
                    type="button"
                    onClick={() => onQueueSteer(turn)}
                    className="rounded px-1 py-px hover:bg-white/10 hover:text-foreground"
                    title={t('bots.queue.steerHint')}
                  >{t('bots.queue.steer')}</button>
                  <button
                    type="button"
                    onClick={() => onQueueRemove(turn.id)}
                    className="rounded p-0.5 hover:bg-white/10 hover:text-rose-300"
                    title={t('bots.queue.cancel')}
                    aria-label={t('bots.queue.cancel')}
                  ><X size={11} /></button>
                </div>
              ))}
              <button
                type="button"
                onClick={onQueueClear}
                className="rounded px-1 py-0.5 text-[10px] text-muted-foreground hover:text-foreground"
              >{t('bots.queue.clear')}</button>
            </div>
          )}
        </div>
      )}

      <div className="glass-composer rounded-2xl border border-white/10 px-2 py-1.5 shadow-sm shadow-black/10 transition-colors focus-within:border-primary/60 focus-within:ring-1 focus-within:ring-primary/25 dark:border-white/5">
        {(contextFiles.length > 0 || attachError) && (
          <div className="flex flex-wrap items-center gap-1.5 px-1 pb-1.5">
            {contextFiles.map((file) => (
              <span
                key={file.name}
                className="inline-flex max-w-full items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10px] text-foreground/80"
                title={file.name}
              >
                <FileText size={10} className="shrink-0 text-primary/80" />
                <span className="max-w-[11rem] truncate">{file.name}</span>
                <button
                  type="button"
                  onClick={() => onRemoveAttachment(file.name)}
                  className="shrink-0 rounded-full p-0.5 text-muted-foreground hover:text-foreground"
                  aria-label={`Remove ${file.name}`}
                >
                  <X size={9} />
                </button>
              </span>
            ))}
            {attachBusy && <Loader2 size={12} className="animate-spin text-muted-foreground" />}
            {attachError && <span className="text-[10px] text-rose-300">{attachError}</span>}
          </div>
        )}

        <div className="flex items-end gap-1">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(event) => onInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                onSend();
              }
            }}
            rows={1}
            data-bot-composer="true"
            placeholder={t('bots.composer.placeholder', { name: botName })}
            aria-label={t('bots.composer.placeholder', { name: botName })}
            wrap={input.length === 0 ? 'off' : 'soft'}
            className="chat-composer-scroll min-h-9 min-w-0 flex-1 resize-none self-center overflow-y-auto border-0 bg-transparent px-2 py-1.5 text-sm leading-relaxed text-foreground outline-none placeholder:text-muted-foreground focus:outline-none focus:ring-0"
          />
          <button
            type="button"
            onClick={onAttach}
            disabled={attachBusy || sending}
            className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-white/10 hover:text-foreground disabled:opacity-40"
            title={t('bots.composer.attach')}
            aria-label={t('bots.composer.attach')}
          >
            <Paperclip size={15} />
          </button>
          {sending ? (
            <button
              type="button"
              onClick={onStop}
              title={t('bots.chat.stop')}
              aria-label={t('bots.chat.stop')}
              className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-full bg-foreground text-background transition-colors hover:bg-foreground/90"
            >
              <Pause size={14} fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={onSend}
              disabled={!input.trim() || attachBusy}
              aria-label={t('bots.composer.send', { name: botName })}
              className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-full bg-primary text-primary-foreground transition-opacity hover:bg-primary/90 disabled:opacity-40 disabled:hover:bg-primary"
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </div>

      {shift && (
        <div className="mt-1.5 hidden px-1 text-right text-[10px] text-muted-foreground sm:block">
          {t('bots.composer.shiftHint')}
        </div>
      )}
    </div>
  );
}
