// ── Lightweight syntax highlighting for the code diff ──────────────────────
// A dependency-free tokenizer, deliberately conservative: comments, strings,
// numbers and a per-language keyword set. Anything it does not recognise stays
// `plain`, so a wrong guess can never change the text the user reads (and the
// line-anchored review comments still quote the exact source). A full grammar
// highlighter (Shiki/TextMate) would add a multi-megabyte dependency for a view
// whose job is reviewing a change, not editing it.

export type TokenKind = 'plain' | 'comment' | 'string' | 'number' | 'keyword' | 'literal';

export interface Token {
  text: string;
  kind: TokenKind;
}

export type Language =
  | 'ts' | 'py' | 'go' | 'rust' | 'java' | 'c' | 'json' | 'css' | 'html'
  | 'shell' | 'sql' | 'md' | 'plain';

const KEYWORDS: Record<Language, Set<string>> = {
  ts: new Set(['import', 'from', 'export', 'default', 'const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while', 'class', 'extends', 'new', 'async', 'await', 'interface', 'type', 'enum', 'switch', 'case', 'break', 'continue', 'try', 'catch', 'finally', 'throw', 'typeof', 'instanceof', 'in', 'of', 'this', 'super', 'static', 'public', 'private', 'protected', 'readonly']),
  py: new Set(['import', 'from', 'as', 'def', 'class', 'return', 'if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally', 'raise', 'with', 'lambda', 'yield', 'async', 'await', 'pass', 'and', 'or', 'not', 'in', 'is', 'global', 'nonlocal', 'assert', 'del']),
  go: new Set(['package', 'import', 'func', 'return', 'if', 'else', 'for', 'range', 'switch', 'case', 'break', 'continue', 'type', 'struct', 'interface', 'map', 'chan', 'go', 'defer', 'select', 'var', 'const']),
  rust: new Set(['use', 'mod', 'fn', 'pub', 'struct', 'enum', 'impl', 'trait', 'let', 'mut', 'const', 'static', 'return', 'if', 'else', 'match', 'for', 'while', 'loop', 'break', 'continue', 'where', 'async', 'await', 'move', 'dyn', 'ref', 'self', 'Self', 'crate', 'super']),
  java: new Set(['package', 'import', 'class', 'interface', 'enum', 'extends', 'implements', 'public', 'private', 'protected', 'static', 'final', 'void', 'return', 'if', 'else', 'for', 'while', 'switch', 'case', 'break', 'continue', 'new', 'try', 'catch', 'finally', 'throw', 'throws', 'this', 'super']),
  c: new Set(['include', 'define', 'struct', 'union', 'enum', 'typedef', 'static', 'const', 'extern', 'void', 'return', 'if', 'else', 'for', 'while', 'switch', 'case', 'break', 'continue', 'sizeof', 'unsigned', 'signed', 'int', 'char', 'float', 'double', 'long', 'short']),
  json: new Set(['true', 'false', 'null']),
  css: new Set(['import', 'media', 'supports', 'keyframes', 'from', 'to']),
  html: new Set([]),
  shell: new Set(['if', 'then', 'else', 'elif', 'fi', 'for', 'in', 'do', 'done', 'while', 'case', 'esac', 'function', 'return', 'export', 'local', 'echo', 'source']),
  sql: new Set(['select', 'from', 'where', 'insert', 'into', 'values', 'update', 'set', 'delete', 'create', 'table', 'alter', 'drop', 'join', 'left', 'right', 'inner', 'outer', 'on', 'group', 'by', 'order', 'having', 'limit', 'as', 'and', 'or', 'not', 'null', 'index', 'primary', 'key', 'foreign', 'references']),
  md: new Set([]),
  plain: new Set([]),
};

const LITERALS = new Set(['true', 'false', 'null', 'None', 'True', 'False', 'nil', 'undefined']);

const EXT_LANGUAGE: Array<[RegExp, Language]> = [
  [/\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/i, 'ts'],
  [/\.pyi?$/i, 'py'],
  [/\.go$/i, 'go'],
  [/\.rs$/i, 'rust'],
  [/\.(java|kt|kts|scala)$/i, 'java'],
  [/\.(c|h|cc|cpp|cxx|hpp|m|mm)$/i, 'c'],
  [/\.jsonc?$/i, 'json'],
  [/\.(css|scss|sass|less)$/i, 'css'],
  [/\.(html?|xml|svg)$/i, 'html'],
  [/\.(sh|bash|zsh|ps1|psm1|bat|cmd)$/i, 'shell'],
  [/\.sql$/i, 'sql'],
  [/\.(md|mdx|markdown)$/i, 'md'],
];

export function languageFor(path: string): Language {
  for (const [pattern, language] of EXT_LANGUAGE) {
    if (pattern.test(path)) return language;
  }
  return 'plain';
}

function commentPrefix(language: Language): string | null {
  if (language === 'py' || language === 'shell') return '#';
  if (language === 'sql') return '--';
  if (language === 'html') return null; // `<!--` handled below
  if (language === 'plain' || language === 'md' || language === 'json') return null;
  return '//';
}

const IDENT = /[A-Za-z_$][A-Za-z0-9_$]*/y;
const NUMBER = /\d[\w.]*/y;

/**
 * Tokenize ONE line. Multi-line strings/comments are not tracked across lines,
 * so a continuation line may show as plain — a display choice, not data loss.
 */
export function tokenizeLine(line: string, language: Language): Token[] {
  if (!line) return [{ text: '', kind: 'plain' }];
  if (language === 'plain') return [{ text: line, kind: 'plain' }];
  const keywords = KEYWORDS[language];
  const tokens: Token[] = [];
  let plain = '';
  let i = 0;

  const flush = () => {
    if (plain) {
      tokens.push({ text: plain, kind: 'plain' });
      plain = '';
    }
  };
  const push = (text: string, kind: TokenKind) => {
    flush();
    tokens.push({ text, kind });
  };

  while (i < line.length) {
    // Whole-line comment.
    const prefix = commentPrefix(language);
    if (prefix && line.startsWith(prefix, i)) {
      push(line.slice(i), 'comment');
      break;
    }
    if (language === 'html' && line.startsWith('<!--', i)) {
      const end = line.indexOf('-->', i + 4);
      const stop = end === -1 ? line.length : end + 3;
      push(line.slice(i, stop), 'comment');
      i = stop;
      continue;
    }
    // Block comment open on this line (one line only).
    if (line.startsWith('/*', i)) {
      const end = line.indexOf('*/', i + 2);
      const stop = end === -1 ? line.length : end + 2;
      push(line.slice(i, stop), 'comment');
      i = stop;
      continue;
    }
    const char = line[i];
    if (char === '"' || char === "'" || char === '`') {
      let j = i + 1;
      while (j < line.length) {
        if (line[j] === '\\') { j += 2; continue; }
        if (line[j] === char) { j += 1; break; }
        j += 1;
      }
      push(line.slice(i, j), 'string');
      i = j;
      continue;
    }
    if (char === '#' && language === 'css') {
      // Hex colour: keep it a literal, not a comment.
      const hex = line.slice(i).match(/^#[0-9a-fA-F]{3,8}\b/);
      if (hex) { push(hex[0], 'literal'); i += hex[0].length; continue; }
    }
    NUMBER.lastIndex = i;
    const num = NUMBER.exec(line);
    if (num && /\d/.test(char)) {
      push(num[0], 'number');
      i += num[0].length;
      continue;
    }
    IDENT.lastIndex = i;
    const ident = IDENT.exec(line);
    if (ident) {
      const word = ident[0];
      if (LITERALS.has(word)) push(word, 'literal');
      else if (keywords.has(word)) push(word, 'keyword');
      else plain += word;
      i += word.length;
      continue;
    }
    plain += char;
    i += 1;
  }
  flush();
  return tokens.length > 0 ? tokens : [{ text: line, kind: 'plain' }];
}
