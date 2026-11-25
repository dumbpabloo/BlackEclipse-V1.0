'use client';

// ─── Canvas left rail ─────────────────────────────────────────────────────────
// Search, create menu and the artifact list. Pure presentation — every action is
// handed to the container, so the rail never fetches on its own.

import {
  AlertTriangle, FileJson, FileText, Loader2, Monitor, PanelLeft, Plus, RefreshCw, Search, SquareCode, Upload,
} from '@/lib/icons';
import type { CanvasKind, CanvasListItem } from '@/lib/harness/clients/canvas-client';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { humanTime, kindLabel } from './canvas-utils';
import { useRef } from 'react';

export interface CanvasRailProps {
  items: CanvasListItem[];
  loading: boolean;
  error: string | null;
  query: string;
  onQueryChange: (query: string) => void;
  onRefresh: () => void;
  onSelect: (id: string) => void;
  onCreate: (kind: CanvasKind) => void;
  onImport: (file: File) => void;
  selectedId: string | null;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

function KindIcon({ kind }: { kind: CanvasKind }) {
  if (kind === 'code') return <SquareCode className="h-4 w-4 shrink-0" />;
  if (kind === 'visual') return <FileJson className="h-4 w-4 shrink-0" />;
  if (kind === 'presentation') return <Monitor className="h-4 w-4 shrink-0" />;
  return <FileText className="h-4 w-4 shrink-0" />;
}

const NEW_ITEMS: { kind: CanvasKind; label: string }[] = [
  { kind: 'document', label: 'Document' },
  { kind: 'code', label: 'Code' },
  { kind: 'visual', label: 'Visual report' },
  { kind: 'presentation', label: 'Presentation' },
];

export function CanvasRail({
  items, loading, error, query, onQueryChange, onRefresh, onSelect, onCreate, onImport,
  selectedId, collapsed, onToggleCollapsed,
}: CanvasRailProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <aside
      aria-label="Canvas artifacts"
      className={cn(
        'flex w-full lg:w-[260px] shrink-0 flex-col gap-3 rounded-2xl glass-card p-3 transition-[width] duration-200',
        collapsed && 'lg:w-[52px] items-center px-1.5',
      )}
    >
      <div className={cn('flex items-center gap-2', collapsed && 'flex-col')}>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={collapsed ? 'Expand artifact list' : 'Collapse artifact list'}
          onClick={onToggleCollapsed}
        >
          <PanelLeft className="h-4 w-4" />
        </Button>
        {!collapsed && <span className="flex-1 text-sm font-semibold text-foreground">Canvas</span>}
        {!collapsed && (
          <Button variant="ghost" size="icon-sm" aria-label="Refresh artifacts" onClick={onRefresh}>
            <RefreshCw className="h-4 w-4" />
          </Button>
        )}
      </div>

      {!collapsed && (
        <>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/50" />
            <Input
              value={query}
              onChange={(e) => onQueryChange(e.target.value)}
              placeholder="Search canvas…"
              aria-label="Search canvas"
              className="pl-8"
            />
          </div>

          <DropdownMenu>
            <DropdownMenuTrigger
              render={<Button variant="secondary" size="sm" className="w-full justify-center" />}
            >
              <Plus className="h-3.5 w-3.5" />
              New
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-[220px]">
              {NEW_ITEMS.map((item) => (
                <DropdownMenuItem key={item.kind} onClick={() => onCreate(item.kind)}>
                  <KindIcon kind={item.kind} />
                  {item.label}
                </DropdownMenuItem>
              ))}
              <DropdownMenuItem onClick={() => fileRef.current?.click()}>
                <Upload className="h-4 w-4 shrink-0" />
                Import file…
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <input
            ref={fileRef}
            type="file"
            className="hidden"
            accept=".md,.markdown,.txt,.json,text/plain,text/markdown,application/json"
            aria-hidden
            tabIndex={-1}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onImport(file);
              e.target.value = '';
            }}
          />
        </>
      )}

      {collapsed && (
        <Button variant="ghost" size="icon-sm" aria-label="New artifact" onClick={() => onCreate('document')}>
          <Plus className="h-4 w-4" />
        </Button>
      )}

      {!collapsed && (
        <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">
          {loading && items.length === 0 ? (
            <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground/70">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
            </div>
          ) : error ? (
            <div className="flex items-start gap-2 rounded-lg border border-rose-500/30 p-2 text-xs text-rose-300">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          ) : items.length === 0 ? (
            <p className="p-3 text-xs text-muted-foreground/60">No canvas artifacts yet.</p>
          ) : (
            items.map((item) => (
              <button
                key={item.artifactId}
                type="button"
                aria-current={item.artifactId === selectedId}
                onClick={() => onSelect(item.artifactId)}
                className={cn(
                  'flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-white/5',
                  item.artifactId === selectedId && 'bg-white/10 ring-1 ring-primary/40',
                )}
              >
                <span className="mt-0.5 text-muted-foreground/70"><KindIcon kind={item.kind} /></span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] text-foreground">{item.name}</span>
                  <span className="block truncate text-[10px] text-muted-foreground/60">
                    {kindLabel(item.kind)} · {humanTime(item.updatedAt)}
                  </span>
                </span>
              </button>
            ))
          )}
        </div>
      )}
    </aside>
  );
}
