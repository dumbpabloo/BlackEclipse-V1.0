'use client';

// One room message. Extracted from `group-chat-room.tsx` so the room file stays
// under the size ratchet; the bubble is presentation only (quote, owner badge,
// task controls and the per-message actions).

import { useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n';
import {
  CheckCircle2, Copy, MessageSquareText, RefreshCw, SquarePen, ThumbsDown, ThumbsUp,
} from '@/lib/icons';
import { BotAvatar } from '@/components/bots/bot-avatar';
import { GhostAvatar } from '@/components/bots/ghost-avatar';
import { Markdown } from '@/components/chat/markdown';
import { ParallelAgentsPanel } from '@/components/chat/parallel-agents-panel';
import { avatarFor, useBotAvatarStore } from '@/lib/bots/avatar-store';
import { formatDateTime } from '@/lib/bots/state';
import type { AutomationView } from '@/lib/harness/clients/automation-browser';
import type { GroupMessage } from '@/lib/bots/groups-store';
import type { GhostSpec } from '@/lib/bots/ghost';

export interface GroupBubbleProps {
  message: GroupMessage;
  groupName: string;
  groupSpec: GhostSpec;
  groupImage?: string;
  onEdit: (id: string) => void;
  replyParent?: GroupMessage;
  members: AutomationView[];
  onReply?: (id: string) => void;
  onReassign?: (message: GroupMessage, ownerId: string) => void;
  onRegenerate?: (id: string) => void;
  onRetry?: (id: string) => void;
  onFeedback?: (id: string, fb: 'up' | 'down') => void;
}

export function GroupBubble({
  message,
  groupName,
  groupSpec,
  groupImage,
  onEdit,
  replyParent,
  members,
  onReply,
  onReassign,
  onRegenerate,
  onRetry,
  onFeedback,
}: GroupBubbleProps) {
  const t = useT();
  const avatars = useBotAvatarStore((s) => s.avatars);
  const [copied, setCopied] = useState(false);
  const isUser = message.role === 'user';
  const avatar = message.authorId
    ? <BotAvatar botId={message.authorId} spec={avatarFor(avatars, message.authorId)} size={28} className="mt-0.5 shrink-0" title={message.authorName} />
    : groupImage
      ? <img src={groupImage} alt="" className="mt-0.5 h-7 w-7 shrink-0 rounded-full object-cover" />
      : <GhostAvatar spec={groupSpec} size={28} className="mt-0.5 shrink-0" />;
  const btn = 'flex h-5 w-5 cursor-pointer items-center justify-center rounded-md hover:bg-white/10 hover:text-foreground';
  const copy = () => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    void navigator.clipboard.writeText(message.content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => { /* clipboard unavailable */ });
  };
  const timeRow = (
    <div className="mt-1 flex items-center gap-0.5 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
      <span className="mr-1">{formatDateTime(message.at)}</span>
      <button type="button" onClick={copy} className={btn} title={copied ? t('bots.chat.copied') : t('bots.chat.copy')} aria-label={t('bots.chat.copy')}>
        {copied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
      </button>
      {!isUser && onFeedback && (
        <>
          <button
            type="button"
            onClick={() => onFeedback(message.id, 'up')}
            className={cn(btn, message.feedback === 'up' && 'text-emerald-300')}
            title={t('bots.chat.good')}
            aria-label={t('bots.chat.good')}
            aria-pressed={message.feedback === 'up'}
          >
            <ThumbsUp size={12} weight={message.feedback === 'up' ? 'fill' : 'regular'} />
          </button>
          <button
            type="button"
            onClick={() => onFeedback(message.id, 'down')}
            className={cn(btn, message.feedback === 'down' && 'text-rose-300')}
            title={t('bots.chat.bad')}
            aria-label={t('bots.chat.bad')}
            aria-pressed={message.feedback === 'down'}
          >
            <ThumbsDown size={12} weight={message.feedback === 'down' ? 'fill' : 'regular'} />
          </button>
        </>
      )}
      {isUser && !message.error && (
        <button type="button" onClick={() => onEdit(message.id)} className={btn} title={t('bots.edit')} aria-label={t('bots.edit')}>
          <SquarePen size={12} />
        </button>
      )}
      {!isUser && onRegenerate && (
        <button type="button" onClick={() => onRegenerate(message.id)} className={btn} title={t('bots.chat.regenerate')} aria-label={t('bots.chat.regenerate')}>
          <RefreshCw size={12} />
        </button>
      )}
      {!isUser && onRetry && (
        <button type="button" onClick={() => onRetry(message.id)} className={btn} title={t('bots.chat.retry')} aria-label={t('bots.chat.retry')}>
          <RefreshCw size={12} />
        </button>
      )}
      {onReply && (
        <button
          type="button"
          onClick={() => onReply(message.id)}
          className={btn}
          title={t('bots.groups.reply')}
          aria-label={t('bots.groups.reply')}
        >
          <MessageSquareText size={12} />
        </button>
      )}
    </div>
  );
  const quote = replyParent ? (
    <div className="mb-1 rounded-lg border-l-2 border-primary/40 bg-white/[0.03] px-2 py-1 text-[10px] text-muted-foreground">
      <span className="font-semibold text-foreground/80">
        {t('bots.groups.replyingTo')} {replyParent.authorName ?? groupName}
      </span>
      <span className="ml-1 line-clamp-2">{replyParent.content.slice(0, 180)}</span>
    </div>
  ) : null;
  const owner = message.taskId === message.id && message.taskOwnerId
    ? members.find((member) => member.id === message.taskOwnerId)
    : undefined;
  const task = message.taskId === message.id && message.taskStatus ? (
    <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
      <span className="rounded-full border border-border/40 px-1.5 py-px">
        {t('bots.groups.owner')}: {owner?.name ?? message.taskOwnerId ?? '—'}
      </span>
      <span className="rounded-full border border-border/40 px-1.5 py-px capitalize">
        {message.taskStatus}
      </span>
      {onReassign && members.length > 0 && (
        <select
          value={message.taskOwnerId ?? ''}
          onChange={(e) => onReassign(message, e.target.value)}
          className="rounded border border-border/40 bg-transparent px-1 py-px text-[10px] text-foreground"
          aria-label={t('bots.groups.reassign')}
        >
          <option value="" disabled>{t('bots.groups.reassign')}</option>
          {members.map((member) => (
            <option key={member.id} value={member.id}>{member.name}</option>
          ))}
        </select>
      )}
    </div>
  ) : null;
  if (!isUser && !message.error) {
    return (
      <div className="group flex gap-2.5">
        {avatar}
        <div className="min-w-0 flex-1">
          <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground">
            <span>{message.authorName ?? groupName}</span>
            {message.scheduled && (
              <span className="rounded-full border border-border/40 px-1.5 py-px text-[9px] font-normal uppercase tracking-wide text-muted-foreground/70">
                {t('bots.chat.autoRun')}
              </span>
            )}
          </div>
          {quote}
          {task}
          {message.delegations && message.delegations.length > 0 && (
            <ParallelAgentsPanel delegations={message.delegations} compact />
          )}
          <Markdown content={message.content} compact className="w-full" />
          {timeRow}
        </div>
      </div>
    );
  }
  return (
    <div className={cn('group flex gap-2.5', isUser ? 'flex-row-reverse' : 'flex-row')}>
      {!isUser && avatar}
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
              <span>{message.authorName ?? groupName}</span>
              {message.scheduled && (
                <span className="rounded-full border border-border/40 px-1.5 py-px text-[9px] font-normal uppercase tracking-wide text-muted-foreground/70">
                  {t('bots.chat.autoRun')}
                </span>
              )}
            </div>
          )}
          {quote}
          {task}
          <div className="whitespace-pre-wrap break-words">{message.content}</div>
        </div>
        {timeRow}
      </div>
    </div>
  );
}
