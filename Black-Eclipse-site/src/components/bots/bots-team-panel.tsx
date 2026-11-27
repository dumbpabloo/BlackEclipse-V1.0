'use client';

// ── Teammates (A2A) ──────────────────────────────────────────────────────────
// Bots mode already runs on the one A2A layer; this panel makes the durable
// teammate record visible instead of leaving it to the Tasks screen. It is a
// read/send surface only: no second agent loop, no fabricated messages.

import { useCallback, useEffect, useState } from 'react';
import { Loader2, MessageSquare, RefreshCw, Users } from '@/lib/icons';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  listA2AAgents,
  listA2AMessages,
  sendA2AMessage,
  type A2AAgentView,
  type A2AMessageView,
} from '@/lib/harness/clients/a2a-client';

function summary(message: A2AMessageView): string {
  const text = message.payload?.text;
  if (typeof text === 'string' && text.trim()) return text.trim().slice(0, 160);
  return message.type.replaceAll('_', ' ').toLowerCase();
}

function when(message: A2AMessageView): string {
  const raw = message.timestamp;
  if (!raw) return '';
  const ms = raw > 1e12 ? raw : raw * 1000;
  return new Date(ms).toLocaleTimeString();
}

export function BotsTeamPanel() {
  const [agents, setAgents] = useState<A2AAgentView[]>([]);
  const [messages, setMessages] = useState<A2AMessageView[]>([]);
  const [recipient, setRecipient] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextAgents, nextMessages] = await Promise.all([
        listA2AAgents(),
        listA2AMessages(),
      ]);
      setAgents(nextAgents.filter((agent) => agent.status !== 'disabled'));
      setMessages(nextMessages.slice(-6).reverse());
      setError(null);
      setRecipient((current) => (
        current || nextAgents.find((agent) => agent.status !== 'disabled')?.id || ''
      ));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not read teammates.');
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);

  const send = async () => {
    const body = text.trim();
    if (!body || !recipient || busy) return;
    setBusy(true);
    setError(null);
    try {
      await sendA2AMessage({ recipientAgentId: recipient, text: body });
      setText('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send the message.');
    } finally {
      setBusy(false);
    }
  };

  const selected = agents.find((agent) => agent.id === recipient);

  return (
    <section className="rounded-xl border border-border/25 bg-white/[0.03] p-2.5">
      <div className="flex items-center gap-1.5">
        <Users size={13} className="text-primary" />
        <h3 className="min-w-0 flex-1 truncate text-[11px] font-semibold text-foreground">
          Teammates
        </h3>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded p-1 text-muted-foreground hover:bg-white/[0.06] hover:text-foreground"
          aria-label="Refresh teammates"
          title="Refresh teammates"
        >
          <RefreshCw size={11} />
        </button>
      </div>
      <p className="mt-1 text-[10px] leading-snug text-muted-foreground">
        Durable A2A messages between the agents behind your bots. Delivery is queued,
        not a live chat turn.
      </p>

      {error && <p className="mt-1.5 text-[10px] text-rose-300">{error}</p>}

      <div className="mt-2 flex flex-wrap gap-1">
        {agents.slice(0, 8).map((agent) => (
          <button
            key={agent.id}
            type="button"
            onClick={() => setRecipient(agent.id)}
            className={`rounded-md border px-1.5 py-0.5 text-[10px] ${
              agent.id === recipient
                ? 'border-primary/50 bg-primary/10 text-foreground'
                : 'border-border/30 text-muted-foreground hover:text-foreground'
            }`}
          >
            {agent.name || agent.id}
          </button>
        ))}
        {agents.length === 0 && (
          <span className="text-[10px] text-muted-foreground">No A2A agents registered.</span>
        )}
      </div>

      <div className="mt-2 space-y-1">
        {messages.map((message) => (
          <div key={message.id} className="flex items-start gap-1.5 text-[10px]">
            <MessageSquare size={10} className="mt-0.5 shrink-0 text-muted-foreground" />
            <span className="min-w-0 flex-1 text-muted-foreground">
              <span className="font-mono text-foreground/80">
                {message.senderAgentId ?? '?'} → {message.recipientAgentId ?? 'all'}
              </span>
              {': '}
              {summary(message)}
              {when(message) && <span className="ml-1 opacity-60">{when(message)}</span>}
            </span>
          </div>
        ))}
        {messages.length === 0 && (
          <p className="text-[10px] text-muted-foreground">No teammate messages yet.</p>
        )}
      </div>

      <div className="mt-2 flex items-center gap-1.5">
        <Input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void send(); }}
          placeholder={selected ? `Message ${selected.name || selected.id}…` : 'Pick a teammate'}
          className="h-7 text-[11px]"
          aria-label="Teammate message"
        />
        <Button
          size="sm"
          className="h-7 rounded-lg px-2"
          onClick={() => void send()}
          disabled={busy || !text.trim() || !recipient}
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : 'Send'}
        </Button>
      </div>
    </section>
  );
}
