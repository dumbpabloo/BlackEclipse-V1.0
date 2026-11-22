'use client';

// React binding for the editor-context store. Kept separate from the pure module
// so the helpers stay DOM-free and unit-testable.

import { useEffect, useState } from 'react';
import {
  editorContextFresh,
  subscribeEditorContext,
  type EditorContext,
} from './editor-context';

export function useEditorContext(): EditorContext | null {
  const [context, setContext] = useState<EditorContext | null>(null);
  useEffect(() => subscribeEditorContext(setContext), []);
  // A stale snapshot is not offered; the editor must publish a newer one.
  return editorContextFresh(context) ? context : null;
}
