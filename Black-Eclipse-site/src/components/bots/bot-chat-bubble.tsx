'use client';

// One bot-chat message: the document-style bot reply, the user/error bubble, and
// the per-message actions (copy, speak, thumbs, reactions, branch). Extracted
// from bot-chat.tsx to keep that surface under the size ratchet.

import { useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import {
  CheckCircle2, Copy, GitBranch, RefreshCw, SquarePen, ThumbsDown, ThumbsUp,
} from '@/lib/icons';
import { Markdown } from '@/components/chat/markdown';
import { TtsButton } from '@/components/chat/tts-button';
import { ParallelAgentsPanel } from '@/components/chat/parallel-agents-panel';
import { BotAvatar } from '@/components/bots/bot-avatar';
import { formatDateTime } from '@/lib/bots/state';
import type { BotMessage } from '@/lib/bots/chat-store';

export function SuggestionChip({ label, onPick }: { label: string; onPick: (v: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onPick(label)}
      className="rounded-full border border-border/50 px-3 py-1 text-[11px] text-muted-foreground hover:border-border hover:text-foreground"
    >
      {label}
    </button>
  );
}

/** A small tag marking an assistant turn that came from an autonomous run
 *  (schedule/event/condition/"Run now") rather than the interactive composer. */
function ScheduledTag() {
  const t = useT();
  return (
    <span className="rounded-full border border-border/40 px-1.5 py-px text-[9px] font-normal uppercase tracking-wide text-muted-foreground/70">
      {t('bots.chat.autoRun')}
    </span>
  );
}

export function ChatBubble({
  message,
  botId,
  botName,
  spec,
  onEdit,
  onRegenerate,
  onRetry,
  onFeedback,
  onReact,
  onBranch,
}: {
  message: BotMessage;
  botId: string;
  botName: string;
  spec: import('@/lib/bots/ghost').GhostSpec;
  onEdit: (id: string) => void;
  onRegenerate?: () => void;
  onRetry?: () => void;
  onFeedback?: (id: string, fb: 'up' | 'down') => void;
  onReact?: (emoji: string) => void;
  onBranch?: () => void;
}) {
  const isUser = message.role === 'user';
  const reactionRow = message.reactions && Object.keys(message.reactions).length
    ? (
      <div className="mt-1 flex flex-wrap items-center gap-1">
        {Object.entries(message.reactions).map(([emoji, count]) => (
          <button
            key={emoji}
            type="button"
            onClick={() => onReact?.(emoji)}
            className="rounded-full border border-border/40 bg-white/[0.04] px-1.5 py-px text-[10px] hover:border-border"
            title={`${emoji} ${count}`}
          >
            {emoji} {count}
          </button>
        ))}
      </div>
    ) : null;
  // A bot reply is the room's/main content: it reads as a full-width document
  // with no glass surface. Only the user's own turns (and honest error turns)
  // are framed as bubbles.
  if (!isUser && !message.error) {
    return (
      <div className="group flex gap-2.5">
        <BotAvatar botId={botId} spec={spec} size={28} className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
            <span>{botName}</span>
            {message.scheduled && <ScheduledTag />}
          </div>
          {message.delegations && message.delegations.length > 0 && (
            <ParallelAgentsPanel delegations={message.delegations} compact />
          )}
          <Markdown content={message.content} compact className="w-full" />
          {reactionRow}
          <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
            <span>{formatDateTime(message.at)}</span>
            <MessageActions
              content={message.content}
              isUser={false}
              feedback={message.feedback}
              onFeedback={onFeedback ? (fb) => onFeedback(message.id, fb) : undefined}
              onReact={onReact}
              onBranch={onBranch}
              onRegenerate={onRegenerate}
            />
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className={cn('group flex gap-2.5', isUser ? 'flex-row-reverse' : 'flex-row')}>
      {!isUser && <BotAvatar botId={botId} spec={spec} size={28} className="mt-0.5 shrink-0" />}
      <div className={cn('flex min-w-0 max-w-[80%] flex-col gap-1', isUser ? 'items-end' : 'items-start')}>
        <div
          className={cn(
            'rounded-2xl px-3.5 py-2 text-sm',
            isUser
              ? 'chat-bubble text-foreground'
              : 'border border-rose-500/30 bg-rose-500/5 text-rose-200',
          )}
        >
          {!isUser && (
            <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
              <span>{botName}</span>
              {message.scheduled && <ScheduledTag />}
            </div>
          )}
          <div className="whitespace-pre-wrap break-words">{message.content}</div>
        </div>
        {reactionRow}
        {/* Time + actions sit below the bubble (not inside it, which made the
            bubble tall) and only appear on hover/focus. */}
        <div className="flex items-center gap-2 px-1 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
          <span>{formatDateTime(message.at)}</span>
          {isUser && !message.error ? (
            <MessageActions content={message.content} isUser onEdit={() => onEdit(message.id)} />
          ) : (
            <MessageActions
              content={message.content}
              isUser={false}
              onReact={onReact}
              onBranch={onBranch}
              onRetry={onRetry}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function MessageActions({
  content,
  isUser,
  feedback,
  onEdit,
  onRegenerate,
  onRetry,
  onFeedback,
  onReact,
  onBranch,
}: {
  content: string;
  isUser: boolean;
  feedback?: 'up' | 'down';
  onEdit?: () => void;
  onRegenerate?: () => void;
  onRetry?: () => void;
  onFeedback?: (fb: 'up' | 'down') => void;
  onReact?: (emoji: string) => void;
  onBranch?: () => void;
}) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const btn =
    'flex h-5 w-5 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-white/10 hover:text-foreground';
  const copy = () => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    void navigator.clipboard.writeText(content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => { /* clipboard unavailable */ });
  };
  return (
    <span className="flex items-center gap-0.5">
      <button
        type="button"
        onClick={copy}
        className={btn}
        title={copied ? t('bots.chat.copied') : t('bots.chat.copy')}
        aria-label={t('bots.chat.copy')}
      >
        {copied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
      </button>
      {!isUser && <TtsButton content={content} />}
      {!isUser && onFeedback && (
        <>
          <button
            type="button"
            onClick={() => onFeedback('up')}
            className={cn(btn, feedback === 'up' && 'text-emerald-300')}
            title={t('bots.chat.good')}
            aria-label={t('bots.chat.good')}
            aria-pressed={feedback === 'up'}
          >
            <ThumbsUp size={12} weight={feedback === 'up' ? 'fill' : 'regular'} />
          </button>
          <button
            type="button"
            onClick={() => onFeedback('down')}
            className={cn(btn, feedback === 'down' && 'text-rose-300')}
            title={t('bots.chat.bad')}
            aria-label={t('bots.chat.bad')}
            aria-pressed={feedback === 'down'}
          >
            <ThumbsDown size={12} weight={feedback === 'down' ? 'fill' : 'regular'} />
          </button>
        </>
      )}
      {isUser && onEdit && (
        <button type="button" onClick={onEdit} className={btn} title={t('bots.edit')} aria-label={t('bots.edit')}>
          <SquarePen size={12} />
        </button>
      )}
      {!isUser && onRegenerate && (
        <button
          type="button"
          onClick={onRegenerate}
          className={btn}
          title={t('bots.chat.regenerate')}
          aria-label={t('bots.chat.regenerate')}
        >
          <RefreshCw size={12} />
        </button>
      )}
      {!isUser && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className={btn}
          title={t('bots.chat.retry')}
          aria-label={t('bots.chat.retry')}
        >
          <RefreshCw size={12} />
        </button>
      )}
      {/* General reactions: a small fixed palette, persisted with the message. */}
      {onReact && ['ðŸ‘', 'ðŸŽ¯', 'â¤ï¸', 'â“'].map((emoji) => (
        <button
          key={emoji}
          type="button"
          onClick={() => onReact(emoji)}
          className={btn}
          title={`${t('bots.chat.react')} ${emoji}`}
          aria-label={`${t('bots.chat.react')} ${emoji}`}
        >
          <span className="text-[11px] leading-none">{emoji}</span>
        </button>
      ))}
      {onBranch && (
        <button
          type="button"
          onClick={onBranch}
          className={btn}
          title={t('bots.threads.branch')}
          aria-label={t('bots.threads.branch')}
        >
          <GitBranch size={12} />
        </button>
      )}
    </span>
  );
}

