'use client';

// ─── Canvas workspace (presentation) ──────────────────────────────────────────
// Assembles the three regions from props only. Holds no data logic, so every
// state (empty, loading, error, loaded) is renderable and testable in isolation.

import { AlertTriangle, Loader2, Redo2, SquareCode } from '@/lib/icons';
import type { CanvasKind } from '@/lib/harness/clients/canvas-client';
import { CanvasAiPanel, type CanvasAiPanelProps } from './canvas-ai-panel';
import { CanvasCenter, type CanvasCenterProps } from './canvas-center';
import { CanvasRail, type CanvasRailProps } from './canvas-rail';
import type { CanvasErrorState } from './canvas-utils';

export interface CanvasWorkspaceProps {
  rail: CanvasRailProps;
  center: CanvasCenterProps | null;
  ai: CanvasAiPanelProps;
  loadingArtifact: boolean;
  artifactError: CanvasErrorState | null;
  onRetryArtifact: () => void;
  onCreate: (kind: CanvasKind) => void;
  /** Presentation editing uses the slide surface; the text-edit AI panel is hidden. */
  hideAi?: boolean;
  /** Inline-in-chat mode: no rail, wrapped in a glass card with a close button. */
  inline?: boolean;
  onClose?: () => void;
}

function EmptyState({ onCreate }: { onCreate: (kind: CanvasKind) => void }) {
  // Creation is chat-driven: Canvas opens from a response ("Open in Canvas") or a
  // saved artifact ("Edit in Canvas"). This is a ready state, not a chooser.
  void onCreate;
  return (
    <section className="flex min-h-0 flex-1 items-center justify-center">
      <div className="glass-card flex max-w-md flex-col items-center gap-2 rounded-2xl p-8 text-center">
        <SquareCode className="h-7 w-7 text-muted-foreground/50" />
        <h2 className="text-lg font-semibold text-foreground">Eclipse Canvas</h2>
        <p className="text-sm text-muted-foreground/70">
          Nothing open yet. Use <span className="text-foreground/80">Open in Canvas</span> on a chat
          response — or <span className="text-foreground/80">Edit in Canvas</span> on a saved artifact —
          to read and edit it here.
        </p>
      </div>
    </section>
  );
}

function ArtifactError({ error, onRetry }: { error: CanvasErrorState; onRetry: () => void }) {
  return (
    <section className="flex min-h-0 flex-1 items-center justify-center" role="alert">
      <div className="glass-card flex max-w-md flex-col items-center gap-3 rounded-2xl p-8 text-center">
        <AlertTriangle className="h-7 w-7 text-rose-400" />
        <p className="text-sm text-rose-200">{error.message}</p>
        {error.status ? <p className="text-[11px] text-muted-foreground/60">Status {error.status}</p> : null}
        <button
          type="button"
          onClick={onRetry}
          className="glass-button flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-foreground"
        >
          <Redo2 className="h-3.5 w-3.5" /> Retry
        </button>
      </div>
    </section>
  );
}

export function CanvasWorkspace({
  rail, center, ai, loadingArtifact, artifactError, onRetryArtifact, onCreate,
  hideAi = false, inline = false, onClose,
}: CanvasWorkspaceProps) {
  const fullWidth = center?.fullWidth ?? false;

  const body = loadingArtifact ? (
    <div className="flex flex-1 items-center justify-center gap-2 text-sm text-muted-foreground/70">
      <Loader2 className="h-4 w-4 animate-spin" /> Loading artifact…
    </div>
  ) : artifactError ? (
    <ArtifactError error={artifactError} onRetry={onRetryArtifact} />
  ) : center ? (
    <CanvasCenter {...center} inline={inline} onClose={onClose} />
  ) : (
    <EmptyState onCreate={onCreate} />
  );

  if (inline) {
    return (
      <section className="glass-card flex h-full min-h-0 w-full flex-col overflow-hidden rounded-2xl">
        <div className="flex min-h-0 flex-1 flex-col gap-3 p-2.5">
          {body}
        </div>
      </section>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
      <CanvasRail {...rail} />
      <main className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">{body}</main>
      {!fullWidth && !hideAi ? <CanvasAiPanel {...ai} /> : null}
    </div>
  );
}
