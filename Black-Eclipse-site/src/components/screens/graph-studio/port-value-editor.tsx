'use client';

// ── Port literal editor ──────────────────────────────────────────────────────
// A node can carry a literal value for an input port, so a step is usable
// without wiring a producer. A wire always wins; this control is the fallback
// source the compiler and executor use. Editing is draft-based for text/JSON.

import { useEffect, useRef, useState } from 'react';
import { X } from '@/lib/icons';
import { cn } from '@/lib/utils';
import type { PortDef } from '@/lib/graph-studio/types';

const INPUT =
  'w-full rounded-md border border-white/10 bg-background/60 px-2 py-1 text-[11px] text-foreground outline-none placeholder:text-muted-foreground/50 focus:border-primary/50';

const JSON_TYPES = new Set([
  'object', 'array', 'ohlcv', 'indicator', 'portfolio', 'order_intent',
  'research_finding', 'evidence', 'agent_result', 'execution_result',
]);

function serialize(value: unknown): string {
  if (value === undefined) return '';
  try {
    return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function PortValueEditor({
  port,
  value,
  wired,
  onChange,
}: {
  port: PortDef;
  value: unknown;
  wired: boolean;
  onChange: (value: unknown | undefined) => void;
}) {
  const [text, setText] = useState(serialize(value));
  const [error, setError] = useState<string | null>(null);
  const focused = useRef(false);
  const isJson = port.type === 'any' || JSON_TYPES.has(port.type);

  useEffect(() => {
    if (!focused.current) setText(serialize(value));
  }, [value]);

  if (wired) {
    return (
      <div className="flex items-center gap-1 rounded-md border border-dashed border-white/15 bg-white/[0.03] px-2 py-1.5 text-[10px] text-muted-foreground">
        <span className="h-1 w-1 rounded-full bg-primary" />
        From wire
        <span className="ml-auto">wire wins</span>
      </div>
    );
  }

  const commitJson = (raw: string) => {
    try {
      onChange(raw.trim() === '' ? undefined : JSON.parse(raw));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Invalid JSON');
    }
  };

  return (
    <div className="space-y-1">
      {port.type === 'boolean' ? (
        <label className="flex h-7 items-center gap-2 text-[11px] text-muted-foreground">
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(event) => onChange(event.target.checked)}
            className="h-3.5 w-3.5 accent-primary"
          />
          {Boolean(value) ? 'true' : 'false'}
          {value !== undefined && (
            <button type="button" onClick={() => onChange(undefined)} className="ml-auto text-muted-foreground/70 hover:text-foreground">
              Clear
            </button>
          )}
        </label>
      ) : port.type === 'number' ? (
        <div className="flex items-center gap-1">
          <input
            type="number"
            value={value === undefined || value === null ? '' : String(value)}
            onChange={(event) => {
              const raw = event.target.value;
              onChange(raw.trim() === '' ? undefined : Number(raw));
            }}
            className={cn(INPUT, 'tabular-nums')}
            placeholder="0"
          />
          {value !== undefined && (
            <button type="button" onClick={() => onChange(undefined)} title="Remove literal" className="shrink-0 rounded p-1 text-muted-foreground hover:text-foreground">
              <X size={11} />
            </button>
          )}
        </div>
      ) : isJson ? (
        <>
          <textarea
            value={text}
            rows={Math.min(8, Math.max(2, text.split('\n').length))}
            spellCheck={false}
            placeholder={port.type === 'array' ? '[]' : '{}'}
            onFocus={() => { focused.current = true; }}
            onChange={(event) => setText(event.target.value)}
            onBlur={() => {
              focused.current = false;
              commitJson(text);
            }}
            className={cn(INPUT, 'resize-y font-mono text-[10.5px]')}
          />
          <div className="flex items-center gap-2 text-[9.5px]">
            {error ? <span className="text-rose-300">{error}</span> : <span className="text-muted-foreground">JSON · commit with Tab</span>}
            {value !== undefined && (
              <button type="button" onClick={() => { onChange(undefined); setText(''); setError(null); }} className="ml-auto text-muted-foreground/70 hover:text-foreground">
                Clear
              </button>
            )}
          </div>
        </>
      ) : (
        <div className="flex items-center gap-1">
          <input
            value={text}
            onFocus={() => { focused.current = true; }}
            onChange={(event) => setText(event.target.value)}
            onBlur={() => {
              focused.current = false;
              onChange(text === '' ? undefined : text);
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                focused.current = false;
                onChange(text === '' ? undefined : text);
                (event.target as HTMLInputElement).blur();
              }
            }}
            placeholder={port.type === 'string' ? 'text value' : port.type === 'ticket' ? '314159 or BTC-USD' : port.type}
            className={INPUT}
          />
          {value !== undefined && (
            <button type="button" onClick={() => { onChange(undefined); setText(''); }} title="Remove literal" className="shrink-0 rounded p-1 text-muted-foreground hover:text-foreground">
              <X size={11} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
