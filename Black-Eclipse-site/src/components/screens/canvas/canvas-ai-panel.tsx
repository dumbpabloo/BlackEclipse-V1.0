'use client';

// ─── Canvas AI panel ──────────────────────────────────────────────────────────
// Ask / Edit / Review / Build proposals against the loaded revision. Everything
// shown here comes from a real `proposeCanvasEdit` response — answer, summary,
// notes, typed operations, an optional result preview and a review diff. Accept
// only applies the proposal through `saveCanvasArtifact`; nothing is faked.

import { AlertTriangle, Check, Loader2, PanelRight, Sparkles, Wand2, X } from '@/lib/icons';
import type { CanvasMode, CanvasProposal } from '@/lib/harness/clients/canvas-client';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { TabBar } from '@/components/ui/tab-bar';
import { diffLines, parseJsonStrict, type CanvasSelection } from './canvas-utils';

export interface CanvasAiPanelProps {
  mode: CanvasMode;
  onModeChange: (mode: CanvasMode) => void;
  instruction: string;
  onInstructionChange: (value: string) => void;
  selection: CanvasSelection | null;
  useSelection: boolean;
  onToggleUseSelection: () => void;
  onPropose: () => void;
  proposing: boolean;
  proposal: CanvasProposal | null;
  onAccept: () => void;
  onDiscard: () => void;
  applying: boolean;
  error: string | null;
  conflictCurrentVersion: number | null;
  onReloadLatest: () => void;
  currentContent: string;
  disabled: boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

const MODES: { id: CanvasMode; label: string }[] = [
  { id: 'ask', label: 'Ask' },
  { id: 'edit', label: 'Edit' },
  { id: 'review', label: 'Review' },
  { id: 'build', label: 'Build' },
];

function ResultPreview({ content }: { content: string }) {
  const parsed = parseJsonStrict(content);
  const text = parsed.ok ? JSON.stringify(parsed.value, null, 2) : content;
  return (
    <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg glass-card-subtle p-2.5 font-mono text-[11px] leading-relaxed text-foreground/85">
      {text}
    </pre>
  );
}

function ReviewDiff({ before, after }: { before: string; after: string }) {
  const lines = diffLines(before, after);
  return (
    <div className="max-h-64 overflow-auto rounded-lg glass-card-subtle p-2 font-mono text-[11px] leading-relaxed">
      {lines.map((line, i) => (
        <div
          key={i}
          className={cn(
            'whitespace-pre-wrap break-words px-1',
            line.type === 'add' && 'bg-emerald-500/10 text-emerald-300',
            line.type === 'del' && 'bg-rose-500/10 text-rose-300',
            line.type === 'ctx' && 'text-muted-foreground/70',
          )}
        >
          {line.type === 'add' ? '+ ' : line.type === 'del' ? '- ' : '  '}
          {line.text || '\u00A0'}
        </div>
      ))}
    </div>
  );
}

export function CanvasAiPanel(props: CanvasAiPanelProps) {
  const {
    mode, onModeChange, instruction, onInstructionChange, selection, useSelection,
    onToggleUseSelection, onPropose, proposing, proposal, onAccept, onDiscard,
    applying, error, conflictCurrentVersion, onReloadLatest, currentContent, disabled,
    collapsed, onToggleCollapsed,
  } = props;

  if (collapsed) {
    return (
      <aside
        aria-label="AI panel (collapsed)"
        className="flex w-full shrink-0 flex-col items-center gap-2 rounded-2xl glass-card p-1.5 lg:w-[52px]"
      >
        <Button variant="ghost" size="icon-sm" aria-label="Expand AI panel" onClick={onToggleCollapsed}>
          <PanelRight className="h-4 w-4" />
        </Button>
        <Sparkles className="h-4 w-4 text-primary" />
      </aside>
    );
  }

  const operationCount = (proposal?.operations.length ?? 0) + (proposal?.deckOperations?.length ?? 0);
  const hasResult = Boolean(proposal && (proposal.answer || proposal.summary || operationCount || proposal.resultContent));
  const showDiff = Boolean(proposal?.resultContent != null && proposal.resultContent !== currentContent);

  return (
    <aside
      aria-label="AI panel"
      className="flex w-full shrink-0 flex-col gap-3 overflow-y-auto rounded-2xl glass-card p-3 lg:w-[360px]"
    >
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-primary" />
        <span className="flex-1 text-sm font-semibold text-foreground">Eclipse AI</span>
        <Button variant="ghost" size="icon-sm" aria-label="Collapse AI panel" onClick={onToggleCollapsed}>
          <PanelRight className="h-4 w-4" />
        </Button>
      </div>

      <TabBar
        variant="subtle"
        className="mb-0"
        items={MODES.map((m) => ({ id: m.id, label: m.label }))}
        activeId={mode}
        onChange={(id) => onModeChange(id as CanvasMode)}
      />

      <textarea
        aria-label="Instruction"
        value={instruction}
        onChange={(e) => onInstructionChange(e.target.value)}
        placeholder="Tell Eclipse what to do…"
        rows={3}
        className="resize-none rounded-lg glass-card-subtle p-2.5 text-sm text-foreground outline-none placeholder:text-muted-foreground/50"
      />

      {selection ? (
        <button
          type="button"
          aria-pressed={useSelection}
          onClick={onToggleUseSelection}
          className={cn(
            'flex items-center gap-1.5 self-start rounded-full px-2.5 py-1 text-[11px] transition-colors',
            useSelection ? 'bg-primary/20 text-foreground ring-1 ring-primary/40' : 'glass-card-subtle text-muted-foreground',
          )}
        >
          <Check className={cn('h-3 w-3', !useSelection && 'opacity-30')} />
          Use selection ({selection.text.length} chars)
        </button>
      ) : null}

      <Button
        variant="default"
        size="sm"
        onClick={onPropose}
        disabled={disabled || proposing || instruction.trim() === ''}
      >
        {proposing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
        {proposing ? 'Proposing…' : 'Propose'}
      </Button>

      {error ? (
        <div className="flex items-start gap-2 rounded-lg border border-rose-500/30 p-2.5 text-xs text-rose-300">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      {conflictCurrentVersion != null ? (
        <div className="flex flex-col gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-xs text-amber-300">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              This artifact changed elsewhere (now revision {conflictCurrentVersion}). Your proposal was not
              saved, and nothing was overwritten.
            </span>
          </div>
          <Button variant="outline" size="sm" className="self-start" onClick={onReloadLatest}>
            Reload latest revision
          </Button>
        </div>
      ) : null}

      {hasResult && proposal ? (
        <div className="flex flex-col gap-3 border-t border-white/10 pt-3">
          {proposal.answer ? (
            <p className="whitespace-pre-wrap text-sm text-foreground/90">{proposal.answer}</p>
          ) : null}
          {proposal.summary ? (
            <p className="text-xs text-muted-foreground/80">{proposal.summary}</p>
          ) : null}
          {proposal.notes.length > 0 ? (
            <ul className="list-disc pl-4 text-[11px] text-muted-foreground/70">
              {proposal.notes.map((note, i) => <li key={i}>{note}</li>)}
            </ul>
          ) : null}

          {proposal.operations.length > 0 ? (
            <div className="flex flex-col gap-1.5">
              <h3 className="text-[11px] uppercase tracking-wide text-muted-foreground/60">
                Operations ({proposal.operations.length})
              </h3>
              {proposal.operations.map((op, i) => (
                <div key={i} className="rounded-lg glass-card-subtle p-2 text-[11px]">
                  <div className="font-mono text-foreground/90">{op.op}</div>
                  {op.explanation ? <div className="mt-0.5 text-muted-foreground/70">{op.explanation}</div> : null}
                  {op.replace ? (
                    <div className="mt-1 font-mono text-[10px] text-muted-foreground/60">
                      replace “{op.search ?? op.expectedText ?? ''}” → “{op.replace}”
                    </div>
                  ) : op.content ? (
                    <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] text-muted-foreground/60">
                      {op.content}
                    </pre>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}

          {proposal.deckOperations && proposal.deckOperations.length > 0 ? (
            <div className="flex flex-col gap-1.5">
              <h3 className="text-[11px] uppercase tracking-wide text-muted-foreground/60">
                Deck operations ({proposal.deckOperations.length})
              </h3>
              {proposal.deckOperations.map((op, i) => (
                <div key={i} className="rounded-lg glass-card-subtle p-2 text-[11px]">
                  <div className="font-mono text-foreground/90">
                    {op.op}{op.slideId ? ` · ${op.slideId}` : ''}
                  </div>
                  {op.explanation ? <div className="mt-0.5 text-muted-foreground/70">{op.explanation}</div> : null}
                  {op.text ? (
                    <div className="mt-1 break-words font-mono text-[10px] text-muted-foreground/60">{op.text}</div>
                  ) : op.bullets ? (
                    <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] text-muted-foreground/60">
                      {op.bullets.join('\n')}
                    </pre>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}

          {proposal.resultContent != null ? (
            <div className="flex flex-col gap-1.5">
              <h3 className="text-[11px] uppercase tracking-wide text-muted-foreground/60">Proposed result</h3>
              <ResultPreview content={proposal.resultContent} />
            </div>
          ) : null}

          {showDiff && proposal.resultContent != null ? (
            <div className="flex flex-col gap-1.5">
              <h3 className="text-[11px] uppercase tracking-wide text-muted-foreground/60">Review diff</h3>
              <ReviewDiff before={currentContent} after={proposal.resultContent} />
            </div>
          ) : null}

          <div className="flex items-center gap-2">
            <Button variant="default" size="sm" onClick={onAccept} disabled={applying}>
              {applying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              {applying ? 'Applying…' : 'Accept'}
            </Button>
            <Button variant="ghost" size="sm" onClick={onDiscard} disabled={applying}>
              <X className="h-3.5 w-3.5" />
              Discard
            </Button>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
