// ── Group-room orchestration (pure) ───────────────────────────────────────────
// Lead routing, bounded shared context and reply-thread helpers. Kept pure so the
// room's behaviour can be tested without React, a model, or the network.

export interface GroupMemberProfile {
  id: string;
  name: string;
  description?: string;
  title?: string;
  objective?: string;
}

export interface RouteDecision {
  ownerId?: string;
  reason: 'keyword' | 'lead' | 'broadcast';
  scores: Record<string, number>;
}

const STOP = new Set([
  'the', 'and', 'for', 'with', 'that', 'this', 'what', 'should', 'could', 'would',
  'please', 'about', 'into', 'from', 'your', 'you', 'our', 'are', 'can', 'how',
  'does', 'did', 'why', 'who', 'when', 'where', 'which', 'all', 'any', 'not',
]);

function tokens(text: string): string[] {
  return (text || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((word) => word.length >= 3 && !STOP.has(word));
}

/**
 * Deterministic lead routing for an unaddressed request.
 *
 * Explicit @mentions are resolved by the caller first. This picks the member
 * whose name/title/description/objective best matches the request; a tie or no
 * signal goes to the lead (who can then assign), and with no lead the request is
 * broadcast to every member.
 */
export function routeGroupRequest(
  text: string,
  members: readonly GroupMemberProfile[],
  leadId?: string | null,
): RouteDecision {
  const words = new Set(tokens(text));
  const scores: Record<string, number> = {};
  for (const member of members) {
    const haystack = new Set(tokens([
      member.name, member.title ?? '', member.description ?? '', member.objective ?? '',
    ].join(' ')));
    let score = 0;
    for (const word of words) if (haystack.has(word)) score += 1;
    scores[member.id] = score;
  }
  const ranked = [...members].sort((a, b) => (scores[b.id] ?? 0) - (scores[a.id] ?? 0));
  const top = ranked[0];
  const second = ranked[1];
  if (top && (scores[top.id] ?? 0) > 0 && (!second || scores[top.id] > (scores[second.id] ?? 0))) {
    return { ownerId: top.id, reason: 'keyword', scores };
  }
  const lead = members.find((member) => member.id === leadId);
  if (lead) return { ownerId: lead.id, reason: 'lead', scores };
  return { reason: 'broadcast', scores };
}

export interface ContextPacketTurn {
  role: 'user' | 'assistant';
  content: string;
  authorName?: string;
  error?: boolean;
}

/**
 * A bounded, self-contained packet for one member's turn. It includes the room,
 * the current request and the newest prior replies within the byte budget; older
 * turns are omitted with an explicit marker, never silently truncated mid-claim.
 */
export function buildContextPacket(input: {
  groupName: string;
  members: readonly string[];
  request: string;
  prior: readonly ContextPacketTurn[];
  budgetChars?: number;
  replyTo?: { author?: string; text: string } | null;
}): string {
  const budget = Math.max(1_000, input.budgetChars ?? 8_000);
  const header = [
    `Room: ${input.groupName}`,
    `Members: ${input.members.join(', ') || 'none'}`,
    input.replyTo
      ? `Thread reply to ${input.replyTo.author || 'a member'}: "${input.replyTo.text.slice(0, 500)}"`
      : '',
    `Current request: ${input.request}`,
  ].filter(Boolean).join('\n');

  const lines: string[] = [];
  let used = header.length;
  let omitted = 0;
  for (let index = input.prior.length - 1; index >= 0; index -= 1) {
    const turn = input.prior[index];
    if (turn.role === 'user') continue; // the current request is already in the header
    const label = turn.authorName || 'Member';
    const body = (turn.content || '').trim();
    if (!body) continue;
    const line = `\n${label}${turn.error ? ' (failed)' : ''}: ${body.slice(0, 1_200)}`;
    if (used + line.length > budget) {
      omitted = index + 1;
      break;
    }
    lines.push(line);
    used += line.length;
  }
  const omittedLine = omitted > 0
    ? `\n[${omitted} older turn(s) omitted for context budget]`
    : '';
  return `${header}${omittedLine}${lines.reverse().join('')}`;
}

/** The member a threaded reply should go back to, if it names an author. */
export function replyOwner(
  messages: readonly { id: string; authorId?: string }[],
  replyToId?: string | null,
): string | undefined {
  if (!replyToId) return undefined;
  return messages.find((message) => message.id === replyToId)?.authorId;
}
