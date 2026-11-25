'use client';

// ─── Canvas editor dispatch ───────────────────────────────────────────────────
// Tiptap and Monaco are SSR-unsafe and heavy, so both editors load through
// `next/dynamic` with `ssr: false`. The visual editor is a plain textarea and
// stays static (and SSR-testable).

import dynamic from 'next/dynamic';
import type { MutableRefObject } from 'react';
import type { CanvasKind } from '@/lib/harness/clients/canvas-client';
import { Loader2 } from '@/lib/icons';
import type { CanvasEditorHandle, CanvasSelection } from './canvas-utils';
import VisualEditor from './visual-editor';

const DocumentEditor = dynamic(() => import('./document-editor'), {
  ssr: false,
  loading: () => <EditorLoading label="Loading rich-text editor…" />,
});

const CodeEditor = dynamic(() => import('./code-editor'), {
  ssr: false,
  loading: () => <EditorLoading label="Loading code editor…" />,
});

const PresentationEditor = dynamic(() => import('./presentation-editor'), {
  ssr: false,
  loading: () => <EditorLoading label="Loading deck editor…" />,
});

function EditorLoading({ label }: { label: string }) {
  return (
    <div className="flex min-h-[40vh] flex-1 items-center justify-center gap-2 rounded-lg glass-card-subtle text-sm text-muted-foreground/70">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label}
    </div>
  );
}

export interface CanvasEditorProps {
  kind: CanvasKind;
  content: string;
  language?: string | null;
  editable?: boolean;
  docKey: string;
  onChange: (next: string) => void;
  onSelectionChange?: (selection: CanvasSelection | null) => void;
  handleRef?: MutableRefObject<CanvasEditorHandle | null>;
}

export function CanvasEditor({
  kind, content, language, editable = true, docKey, onChange, onSelectionChange, handleRef,
}: CanvasEditorProps) {
  if (kind === 'document') {
    return (
      <DocumentEditor
        content={content} editable={editable} docKey={docKey}
        onChange={onChange} onSelectionChange={onSelectionChange} handleRef={handleRef}
      />
    );
  }
  if (kind === 'code') {
    return (
      <CodeEditor
        content={content} language={language} editable={editable}
        onChange={onChange} onSelectionChange={onSelectionChange} handleRef={handleRef}
      />
    );
  }
  if (kind === 'presentation') {
    return (
      <PresentationEditor
        content={content} editable={editable} docKey={docKey}
        onChange={onChange} handleRef={handleRef}
      />
    );
  }
  return <VisualEditor content={content} editable={editable} onChange={onChange} onSelectionChange={onSelectionChange} />;
}
