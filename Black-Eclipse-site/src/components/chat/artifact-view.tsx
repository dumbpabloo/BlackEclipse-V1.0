'use client';

import { useEffect, useState } from 'react';
import { Download } from '@/lib/icons';
import { useT } from '@/lib/i18n/use-t';
import { listArtifacts, downloadUrl, inlineUrl, thumbnailUrl, type Artifact } from '@/lib/harness/clients/artifacts-browser';
import type { ComputedArtifact } from '@/lib/harness/analysis-types';
import { downloadFromUrlInShell } from '@/lib/desktop/download';
import { VideoPlayer } from '@/components/media/video-player';

// ─── Phase 15/18 artifact view ──────────────────────────────────────────────
// A produced file the run handed over: an IMAGE renders inline through the
// Artifact Center's own download route (never a caller-supplied path), anything
// else is a download — and when the catalog has no entry for it, the honest
// fallback is the name + digest with NO link (a broken link would pretend the
// file is retrievable when it is not).

/** Index a run's catalog entries by artifact id, checksum and name. */
export type ArtifactIndex = Record<string, Artifact>;

export function indexArtifacts(artifacts: Artifact[]): ArtifactIndex {
  const index: ArtifactIndex = {};
  for (const artifact of artifacts) {
    index[artifact.id] = artifact;
    if (artifact.checksum) index[`sha:${artifact.checksum}`] = artifact;
    index[`name:${artifact.name}`] = artifact;
  }
  return index;
}

export function entryFor(index: ArtifactIndex, artifact: ComputedArtifact): Artifact | null {
  return index[`sha:${artifact.sha256}`] ?? index[`name:${artifact.name}`] ?? null;
}

/** Resolve a run's Artifact Center entries (client-side; empty on failure). */
export function useRunArtifactIndex(runId?: string): ArtifactIndex {
  const [index, setIndex] = useState<ArtifactIndex>({});
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    (async () => {
      try {
        const page = await listArtifacts({ runId, limit: 100 });
        if (!cancelled) setIndex(indexArtifacts(page.artifacts));
      } catch {
        // No catalog entry resolves — the fallback (name + digest, no link) renders.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);
  return index;
}

export function ArtifactView({
  artifact,
  entry,
  compact = false,
}: {
  artifact: ComputedArtifact;
  entry?: Artifact | null;
  compact?: boolean;
}) {
  const t = useT();
  const mime = (entry?.mimeType ?? artifact.mediaType ?? '').toLowerCase();

  if (entry && mime.startsWith('image/')) {
    return (
      <figure className="mt-1">
        {/* The bytes come from the Artifact Center route (derived server-side). */}
        <img
          src={downloadUrl(entry.id)}
          alt={artifact.name}
          className="max-h-72 w-auto rounded-lg border border-border/30"
        />
        <figcaption className="mt-0.5 text-[9px] text-muted-foreground/70">
          {artifact.name}
        </figcaption>
      </figure>
    );
  }

  if (entry && mime.startsWith('video/')) {
    // A produced video plays inline through the ONE shared player; when the
    // codec cannot play here it offers the file instead of a dead box.
    return (
      <figure className="mt-1">
        <VideoPlayer
          src={inlineUrl(entry.id)}
          poster={entry.thumbnailKey ? thumbnailUrl(entry.id) : undefined}
          downloadHref={downloadUrl(entry.id)}
          fileName={artifact.name}
          onDownload={(event) => downloadFromUrlInShell(event, downloadUrl(entry.id), artifact.name)}
          compact
          label={`Video: ${artifact.name}`}
        />
        <figcaption className="mt-0.5 text-[9px] text-muted-foreground/70">
          {artifact.name}
        </figcaption>
      </figure>
    );
  }

  if (entry) {
    const href = downloadUrl(entry.id);
    return (
      <a
        href={href}
        onClick={(event) => {
          // The desktop window has no download shelf; ask the shell to save it.
          downloadFromUrlInShell(event, href, artifact.name);
        }}
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-white/5 hover:text-foreground"
      >
        <Download size={11} />
        <span>{t('artifacts.download')}</span>
        <span className={compact ? 'sr-only' : undefined}>· {artifact.name}</span>
      </a>
    );
  }

  // Honest fallback: no catalog entry means no link at all.
  return (
    <span className="inline-flex items-center gap-1.5">
      <span>{artifact.name}</span>
      <span className="font-mono text-[9px] text-muted-foreground/60">
        {artifact.sha256.slice(0, 12)}
      </span>
    </span>
  );
}
