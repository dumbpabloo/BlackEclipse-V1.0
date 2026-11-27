import { describe, expect, it } from 'vitest';

import { isQuietAnswer } from './quiet';

describe('isQuietAnswer', () => {
  it('accepts the exact quiet markers, including surrounding whitespace', () => {
    expect(isQuietAnswer('[[no-update]]')).toBe(true);
    expect(isQuietAnswer('  [SILENT]\n')).toBe(true);
  });

  it('never hides a real report that merely mentions a marker', () => {
    expect(isQuietAnswer('Nothing changed. [[no-update]]')).toBe(false);
    expect(isQuietAnswer('The feed was [SILENT] for two hours; here is what I found.')).toBe(false);
  });

  it('treats empty/absent output as not quiet', () => {
    expect(isQuietAnswer(null)).toBe(false);
    expect(isQuietAnswer(undefined)).toBe(false);
    expect(isQuietAnswer('   ')).toBe(false);
  });
});
