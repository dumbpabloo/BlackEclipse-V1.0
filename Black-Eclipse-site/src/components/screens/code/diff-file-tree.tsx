'use client';

// ── Change tree ─────────────────────────────────────────────────────────────
// The per-file navigator for the Code-screen diff: directories, a status mark
// and the line counts, so a multi-file change is scannable before any hunk is
// opened. Selection filters the diff body to one file.

import { useEffect, useState } from 'react';
import { cn } from '@/lib/utils';
import { useT } from '@/lib/i18n/use-t';
import { FolderOpen } from '@/lib/icons';
import { absoluteWorkspacePath, hasRevealBridge, revealInOs } from '@/lib/desktop/reveal';
import type { DiffFile } from '@/lib/code/diff-model';
import { groupFilesByDirectory } from '@/lib/code/diff-model';

export interface DiffFileTreeProps {
  files: DiffFile[];
  selected: string | null;
  onSelect: (path: string | null) => void;
  /** Absolute workspace folder, so a file can be revealed in the OS. */
  workspaceRoot?: string;
}

const STATUS_MARK: Record<DiffFile['status'], string> = {
  added: 'A',
  modified: 'M',
  deleted: 'D',
};

const STATUS_CLASS: Record<DiffFile['status'], string> = {
  added: 'text-primary',
  modified: 'text-sky-300',
  deleted: 'text-destructive',
};

export function DiffFileTree({ files, selected, onSelect, workspaceRoot }: DiffFileTreeProps) {
  const t = useT();
  const [canReveal, setCanReveal] = useState(false);
  useEffect(() => { setCanReveal(hasRevealBridge()); }, []);
  if (files.length === 0) return null;
  const groups = groupFilesByDirectory(files);
  const totalAdd = files.reduce((sum, f) => sum + f.additions, 0);
  const totalDel = files.reduce((sum, f) => sum + f.deletions, 0);
  return (
    <div className="glass-card-subtle rounded-lg border border-border/40 p-1.5 text-[10.5px]">
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={cn(
          'flex w-full items-center justify-between rounded px-1.5 py-0.5 text-left',
          selected === null ? 'bg-accent/60 text-foreground' : 'text-muted-foreground hover:bg-accent/30',
        )}
      >
        <span>{t('code.diff.allFiles', { count: files.length })}</span>
        <span className="font-mono">
          {totalAdd > 0 && <span className="text-primary">+{totalAdd}</span>}
          {totalDel > 0 && <span className="ml-1 text-destructive">-{totalDel}</span>}
        </span>
      </button>
      <div className="mt-1 space-y-1">
        {groups.map((group) => (
          <div key={group.directory}>
            <div className="truncate px-1.5 font-mono text-muted-foreground/50">{group.directory}/</div>
            {group.files.map((file) => {
              const name = file.path.slice(file.path.lastIndexOf('/') + 1);
              const active = selected === file.path;
              return (
                <div key={file.path} className="group flex items-center gap-0.5">
                  <button
                    type="button"
                    onClick={() => onSelect(file.path)}
                    title={file.path}
                    className={cn(
                      'flex min-w-0 flex-1 items-center gap-1.5 rounded px-1.5 py-0.5 text-left font-mono',
                      active ? 'bg-accent/60 text-foreground' : 'text-muted-foreground hover:bg-accent/30',
                    )}
                  >
                    <span className={cn('w-2.5 shrink-0', STATUS_CLASS[file.status])}>{STATUS_MARK[file.status]}</span>
                    <span className="min-w-0 flex-1 truncate">{name}</span>
                    <span className="shrink-0 text-[9.5px]">
                      {file.additions > 0 && <span className="text-primary">+{file.additions}</span>}
                      {file.deletions > 0 && <span className="ml-0.5 text-destructive">-{file.deletions}</span>}
                    </span>
                  </button>
                  {canReveal && workspaceRoot && (
                    <button
                      type="button"
                      onClick={() => {
                        const target = absoluteWorkspacePath(workspaceRoot, file.path);
                        if (target) revealInOs(target);
                      }}
                      title={t('code.diff.reveal')}
                      aria-label={t('code.diff.reveal')}
                      className="shrink-0 rounded p-0.5 text-muted-foreground/40 hover:bg-accent/40 hover:text-foreground"
                    >
                      <FolderOpen size={10} />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
