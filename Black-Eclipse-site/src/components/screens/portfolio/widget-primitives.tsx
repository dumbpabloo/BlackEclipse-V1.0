'use client';

// ── Shared fund-widget primitives ────────────────────────────────────────────
// Small presentational building blocks used by widgets.tsx and any widget
// split into its own file. Keeping them here lets large widget modules be
// divided without duplicating chrome.

import React from 'react';
import { cn } from '@/lib/utils';
import type { FundData } from './use-fund-data';
import type { Fund } from '@/lib/portfolio/types';

export interface WidgetProps {
  fund: Fund;
  data: FundData;
}

export function WidgetHeader({ icon: Icon, title, hint }: { icon: React.ComponentType<{ size?: number; className?: string }>; title: string; hint?: string }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border/10 pb-2.5 mb-3">
      <div className="flex items-center gap-2 min-w-0">
        <Icon size={14} className="text-primary shrink-0" />
        <h3 className="font-serif text-sm font-semibold text-foreground truncate">{title}</h3>
      </div>
      {hint && <span className="font-mono text-[9px] text-muted-foreground/70 shrink-0">{hint}</span>}
    </div>
  );
}

export function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center py-8 text-center">
      <p className="text-xs text-muted-foreground/70 italic">{children}</p>
    </div>
  );
}

export const Tile = React.forwardRef<HTMLDivElement, {
  label: string; value: string; tone?: string; sub?: string; loading?: boolean;
}>(function Tile({ label, value, tone, sub, loading }, ref) {
  return (
    <div ref={ref} className="flex min-w-0 flex-col justify-center rounded-xl glass-card-subtle p-3">
      <p className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground truncate">{label}</p>
      {loading ? (
        <span className="mt-1.5 block h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground/70" aria-hidden />
      ) : (
        <>
          <p className={cn('mt-1 font-mono text-sm font-bold truncate', tone ?? 'text-foreground')}>{value}</p>
          {sub && <p className={cn('font-mono text-[10px] truncate', tone ?? 'text-muted-foreground')}>{sub}</p>}
        </>
      )}
    </div>
  );
});
