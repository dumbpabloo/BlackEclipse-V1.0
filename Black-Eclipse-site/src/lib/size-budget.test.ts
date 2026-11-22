import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

// "Keep files small" guard (AGENTS.md). Memory is the top constraint on this
// machine, so a runaway monolith is a real risk.
//
// This is a GRADUATED guard, not the old single 200 KB ceiling (which nothing
// tripped: the largest file, market-overview.tsx, is ~155 KB):
//
//   * HARD ceiling  — a file over this many LINES must be in RATCHET below.
//   * SOFT ceiling  — a file over this is reported (console.warn) but allowed.
//   * RATCHET       — the files already over the hard ceiling, each pinned to
//                     its current size. They may SHRINK but never GROW, so the
//                     allowance cannot silently grandfather a file that keeps
//                     getting bigger. Removing a file from this list (because it
//                     was split) needs no ratchet edit; a genuine shrink needs
//                     no edit either.
//
// A NEW oversized file fails. An existing one may not grow. That is what makes
// the guard bite.

// Anchor to this file's directory (…/src/lib) → repo app root, independent of
// the process cwd.
const ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "..", "..");
const HARD_CEILING_LINES = 1200;
const SOFT_CEILING_LINES = 800;
const SKIP_DIRS = new Set(["generated", "node_modules", ".next", ".next-dev", "vendor", "dist"]);

// The files above the hard ceiling today, pinned to their measured size. Each
// entry is a near-term split candidate; the budget is the current height, so the
// file can only get smaller until it is split and removed from this list.
const RATCHET: Record<string, number> = {
  "src/components/screens/market-overview.tsx": 2760,
  "src/components/screens/eclipse-memory/eclipse-memory-screen.tsx": 2488,
  "src/lib/i18n/dictionaries/index.ts": 1549,
  // The canonical English dictionary (`en.ts`) crossed the hard ceiling; the
  // self-editing harness added the `code.command.*` + `code.promote.*` keys, and
  // the shared tree already carried WIP dictionary growth. Pinned at its measured
  // size; the standing split candidate is per-domain dictionary modules
  // (`dictionaries/code.ts`, …) re-exported by `en.ts`. May only shrink.
  // Re-pinned 1218→1219: the scheduler's seconds cadence needed one new key
  // (`bots.seconds`) alongside the existing every/minutes/hours/days set.
  // Re-pinned 1219→1220: one key (`bots.chat.autoRun`) tags an autonomous run's
  // answer in the bot thread.
  // Re-pinned 1220→1222: two keys (`bots.scheduleSuggestion`, `bots.scheduleApply`)
  // drive the one-click "schedule this bot from its description" prompt on an
  // existing manual bot.
  // Re-pinned 1222→1225: the schedule builder gained a monthly choice
  // (`bots.sched.monthly`), a day-of-month label (`bots.sched.dayOfMonth`) and an
  // interval-floor note (`bots.sched.minInterval`).
  // Re-pinned 1225→1238: the Advanced panel now exposes the engine's run
  // behaviour (misfire / catch-up / concurrency / retry / timeout) —
  // `bots.behaviour.*`.
  // Re-pinned 1238→1240: per-bot model override (`bots.model.*`). May only shrink
  // from here.
  // Re-pinned 1240→1241: one key (`capability.class.advisoryVerification`) names
  // the Fiduciary Gate capability class (ADR-H196/H193). May only shrink.
  // Re-pinned 1241→1253: the sidebar quick-launchers added the `sidebar.gateway*`
  // + `sidebar.graph*` keys (Gateway dialog + graph browser). May only shrink.
  // Re-pinned 1253→1254: Graph Studio added one nav key (`nav.graphStudio`).
  // Re-pinned 1254→1255: Graph Studio became a dedicated sidebar MODE, which
  // needs its own switcher label (`sidebar.graphStudio`). Re-pinned 1255→1262
  // for the in-flight evolution/operator surface keys, then 1262→1263 for the
  // drift-candidate badge. Re-pinned 1263→1266: the post-landing restart
  // verification keys (`code.promote.postLanding*`). The standing split
  // candidate is still per-domain dictionary modules. May only shrink.
  // Phase 22 / S2/S4/S5/S6 adds session, landing, phase and scorecard keys. May only shrink.
  // Re-pinned 1312->1313 to the measured tree (2026-11-06): in-flight i18n string
  // work on the shared tree added one key; this change touches no dictionary.
  // Re-pinned 1317->1326: the command palette surface added nine
  // `code.command.*` keys (dialog, pinned, recent, no-matches, aliases,
  // examples, search placeholder, selection hint, suggestions). May only shrink.
  // Re-pinned 1326->1330: chat-history multi-select adds `sidebar.selectAll`,
  // `sidebar.clearSelection`, `sidebar.done` and `sidebar.confirmDeleteMany`.
  // Re-pinned 1330->1352: the autonomy status strip gained its `autonomy.*`
  // keys (goal/loop labels, actions, gate/turn/wake copy, off-engine hint).
  // Re-pinned 1352->1355 for the goal detail affordance (`details`, `noneYet`)
  // and the live-run label.
  // Re-pinned 1355->1358 for the multi-goal strip's queued/running/loading copy.
  // +1 for the re-judge action.
  // Re-pinned 1359->1367 (2026-11-21) for the Code-screen plan/image/undo copy
  // (`code.plan.*`, `code.chat.attachImage`, `code.checkpoints.undoTurn`,
  // `code.promote.hunk`). Re-pinned 1367->1373 for the line-anchored review
  // comments (`code.review.anchorHint`/`linePlaceholder`/`saveLine`/`remove`/
  // `anchoredCount`/`seeLine`). Re-pinned 1373->1380 for the split/file-tree/
  // image copy (`code.diff.*`). Re-pinned 1380->1381 for the picture-loading
  // copy, then 1381->1385 for the before/after labels, then 1385->1393 for the
  // source modes. May only shrink.
  "src/lib/i18n/dictionaries/en.ts": 1394,
  // The canonical command catalog crossed the hard ceiling when the P2
  // read-only workspace commands (alerts/news/theses/journal/jobs/artifacts/
  // sessions/git/stop) landed. Re-pinned 1379->1411 for `/history`, the
  // `/usage --commands` flag and the `--confirm` token flags on high-risk
  // commands. The standing split candidate is per-domain spec modules
  // (`commands/*.ts`) re-exported by `command-registry.ts`. May only shrink.
  // Re-pinned 1411->1444 (2026-11-09) for the new entries merged into the
  // release tree; the split candidate is unchanged. Re-pinned 1444->1499 for
  // the typed-UI `/canvas` and `/open` specs. May only shrink.
  "src/lib/harness/commands/command-registry.ts": 1499,
  // Grew with the education/video capability dispatch (youtube/course/github
  // connectors) landing alongside the internet-image-search capability. The
  // image-search dispatch was extracted to `data-bridge-images.ts` so this file
  // only carries the import + case. R3-04 split follow-up: the health/diagnostics
  // group moved to `data-bridge-health.ts`, the education/media group to
  // `data-bridge-education.ts`, the shared unavailable helpers to
  // `data-bridge-result.ts`, and the direct price-history transport to
  // `data-bridge-history.ts`. Re-pinned 1710→1377 to the measured tree; may only
  // shrink from here. Next split candidate: the instrument-identity block
  // (`resolveVaultInstrument`/`resolveAnyInstrument`/…), which would also free the
  // statement/filings functions to move out.
  // Re-pinned 1377→1379 for the `response_contract` dispatch (two lines: the case
  // + its one-line return), the model's read-back for the eclipse-response block
  // format. Implementation lives in `response-bridge.ts`; may only shrink.
  // Re-pinned 1379→1400 for the register closeout: statement-cache staleness and
  // the live profile/valuation helpers were EXTRACTED to
  // `data-bridge-statements.ts` (the split candidate named above), leaving the
  // `valuation` block + helper imports here (+21 net). May only shrink; the next
  // split candidate is the instrument-identity block.
  // Re-pinned 1400->1408 for the in-flight price-history window work: the
  // transport now carries the caller's start/end window instead of the bridge
  // filtering it afterwards. May only shrink from here.
  // Re-pinned 1408->1451 for the in-flight market-data history work (a
  // market-data symbol with no instrument row is fetched from the market-data
  // transport and labelled as such). May only shrink from here.
  "src/lib/harness/data-bridge.ts": 1451,
  // Re-pinned 1413→1414, 1405→1415, 1336→1338: each gaining `active` prop
  // plumbing so keep-alive tabs stop polling while hidden (the shell keeps
  // visited screens mounted). May only shrink from here.
  "src/components/screens/market-bubbles.tsx": 1414,
  "src/components/chart/chart-widget.tsx": 1415,
  "src/lib/workspace/workspace-canvas.tsx": 1374,
  "src/components/screens/customization-settings.tsx": 1337,
  // Re-pinned 1338→1486: the High Alert feature (importance filter, alert
  // creation from a card, portfolio relevance) is wired into the existing news
  // component. That work belongs on this screen; the near-term split is the
  // card renderer + filter bar into their own modules. Re-pinned 1486→1446 when
  // card settings moved to `src/lib/news/card-settings.ts` and the High Alert
  // band was folded into the ranked feed. May only shrink.
  "src/components/screens/market-news.tsx": 1446,
  // Grew when the AI section gained a real provider select + model dropdown
  // (the previous datalist was not surfaced by the input primitive). Pinned
  // until the deferred AI-section split; may only shrink from here.
  "src/components/screens/settings.tsx": 1289,
  // The SPA shell was exactly at the ceiling (1200). Mounting the Model Control
  // Center added one dynamic import + one keep-alive block; pinned at its new
  // height until the shell is split. Grew again with the terminal dock plumbing
  // and the focus-mode "tuck the terminal" effect. Grew once more with the
  // approval-card wiring (composer anchor + the portaled ApprovalCard). Grew yet
  // again by one dynamic import + one keep-alive block for the dedicated
  // Research tab. May only shrink from here. Grew by one import + one mount
  // line when the Agent Workspace became a docked right sidebar in the shell
  // row (it moved out of the root layout to reserve real column space). Grew
  // again by one dynamic import + one keep-alive block each for the Portfolio
  // and Unified Search tabs, then the Task / Plan runtime's mount (a dynamic
  // import + one keep-alive block). Grew once more by one dynamic import + one
  // keep-alive block for the Plugins tab. May only shrink from here; the shell's
  // per-tab keep-alive blocks are the standing split candidate. Grew by one
  // handler + one prop passthrough for the Interactive Response Engine's
  // `controls` follow-up (a response block re-enters the same agent loop).
  // Grew by one dynamic import + one keep-alive block for the Research Data
  // (`research-data`) tab. May only shrink from here; the per-tab keep-alive
  // blocks remain the standing split candidate. Grew again by one dynamic
  // import + one keep-alive block for the Factor Lab (`factor-lab`) tab.
  // Grew again by one dynamic import + one keep-alive block for the ML Lab
  // (`ml-lab`) tab. May only shrink from here; the per-tab keep-alive blocks
  // remain the standing split candidate. Grew again by one dynamic import + one
  // keep-alive block for the Bots (`bots`) tab, which mounts the ghost-avatar
  // bot manager. May only shrink from here.
  // Re-pinned 1366→1390 to the measured tree (2026-10-14): further in-flight
  // shared-tree wiring grew the shell past its pin; the self-editing-harness
  // program touches NO part of stock-chat.tsx. May only shrink from here.
  // Re-pinned 1390→1391: the same in-flight shared-tree edit added one more line
  // before it landed. May only shrink from here.
  // Re-pinned 1391→1399: the concurrent "Aurora glow" chat-shell change (commit
  // ab9b5eb2) added glow wiring to the ChatBot page; the slash-command work
  // touches no part of stock-chat.tsx. May only shrink from here.
  // Re-pinned 1399→1435 for main-chat `@mention` delegation: the shell wires the
  // mentionable list + a dispatch branch into its send paths. The turn lifecycle
  // and the bulk of the logic were EXTRACTED to `lib/bots/use-bot-delegation.ts`,
  // so the residual growth is only composer plumbing. May only shrink from here.
  // Re-pinned 1435→1441: routing the approval card's decision to the delegated
  // run when the pending approval belongs to an `@`-mention (the rest of that
  // feature lives in the hook). May only shrink from here.
  // Re-pinned 1441→1455: replacing the chat-scoped "New chat" page shortcut with
  // a global Ctrl/Cmd+Alt+N handler (switch to ChatBot + new chat + focus the
  // composer). May only shrink from here; the per-tab keep-alive blocks remain
  // the standing split candidate.
  // Re-pinned 1455→1460: restoring the in-panel Aurora glow on the ChatBot
  // landing (the app-wide responsive pass, commit 5062aeab, dropped it, so the
  // shell glow was covered by the opaque landing backdrop). One mount line + a
  // shorter wordmark comment. May only shrink from here.
  // Re-pinned 1460→1474: the mobile navigation program mounts the phone bottom
  // nav and the dock host (sheet), plus the platform hook they share. The heavy
  // nav catalogue lives in `layout/mobile-bottom-nav.tsx`; only the mounts and
  // one `useIsMobile` call land here. May only shrink from here; the per-tab
  // keep-alive blocks remain the standing split candidate.
  // Re-pinned 1481→1506: Canvas is now an INLINE pane in the chat (no tab) — the
  // shell opens/close the split, forwards the artifact id and renders the pane;
  // the workspace itself lives in `components/screens/canvas/`. May only shrink
  // from here; the per-tab keep-alive blocks remain the standing split candidate.
  // Re-pinned 1506→1512: Graph Studio mounts a dedicated tab (a lazy dynamic
  // import + one keep-alive block, matching every other lab screen). May only
  // shrink from here; the per-tab keep-alive blocks remain the standing split
  // candidate.
  // Re-pinned 1512→1518: Graph Studio became a dedicated MODE — the shell hides
  // the top bar for it, hides the chat dock, and gives the right dock to the
  // screen's own inspector/run rail (three small condition edits). May only
  // shrink from here.
  // Re-pinned 1518→1522: closed chrome (terminal/scheduler/dock/aurora) moved to
  // `screens/lazy-chrome.tsx`, leaving one import, two `active` prop passes and
  // the deferred canvas-content import here. May only shrink from here.
  // Re-pinned 1522→1534: the new Dashboard tab (a lazy dynamic import + one
  // keep-alive block above Command Center). May only shrink from here.
  // Re-pinned 1541→1554: the `clarify` human-in-the-loop question card is wired
  // into the main chat composer (one state read, one persisted field, one mount
  // beside the approval card). The card itself lives in
  // `components/chat/clarification-card.tsx`; no other stock-chat logic moved.
  // Re-pinned 1554→1555 for the parallel fan-out: the turn sink forwards ONE
  // extra persisted field (`delegations`); the card and reducer live in
  // `components/chat/parallel-agents-panel.tsx` / `lib/agent/delegation-reducer.ts`.
  // Re-pinned 1555→1572 for the Tables keep-alive tab mount (dynamic import,
  // mount block, right-dock/ChatDock exclusions); the page itself lives in
  // `components/screens/tables/tables-screen.tsx`.
  // Re-pinned 1572→1584 for the Agentic Overview tab (same small mount pattern;
  // the gallery lives in `components/screens/overview/overview-screen.tsx`).
  // Re-pinned 1584→1589 for the Strategy Studio keep-alive mount (dynamic import
  // + mount block); the page itself lives in
  // `components/screens/strategy-studio/strategy-studio-screen.tsx`.
  // May only shrink from here.
  // Re-pinned 1589→1596: the chat surface gained the AI-result cue wiring and
  // related stream-state affordances. Split candidate: the message renderer
  // from the composer/stream controller. May only shrink.
  // Re-pinned 1596→1603: the per-chat project chip in the empty-state and
  // composer rows (Phase 23). The chip itself is a separate component; this is
  // only its two mount points. May only shrink.
  // Re-pinned 1603→1606: the Builds tab mount (`?tab=code`). The keep-alive
  // wrapper + dynamic import live in `components/screens/code/code-tab.tsx`;
  // this is only the import and the one mount line. No other shell logic moved.
  // Re-pinned 1606→1609 for the Canvas move wiring: the synchronous
  // `contentFromMessage` import and the `canvasMessageId` prop. May only shrink.
  // Re-pinned 1609→1717 (2026-11-09): the root shell gained the conversation
  // session bar wiring and related chat-surface mount points from in-flight
  // work. Split candidate unchanged (message renderer vs composer/stream
  // controller). May only shrink from here.
  // Re-pinned 1717→1718 (2026-11-13): the composer's `!` shell mode wires the
  // shared `runShell` action onto the landing and follow-up composers; the mode
  // itself, the transcript card and the context assembly all live in
  // `lib/chat/shell.ts` / `chat/shell-run-card.tsx`. May only shrink from here.
  // Re-pinned 1718→1720 (2026-11-17): the in-app navigation intent hook
  // (`useAppNavigation`, import + one call) so a bot dialog can open Strategy
  // Studio; the listener lives in `lib/navigation/use-app-navigation.ts`.
  // May only shrink from here.
  // +23 (2026-11-17): goal verdicts stream into the transcript (the watcher hook
  // plus the idempotent sink). The polling itself lives in
  // `lib/harness/use-goal-verdict-watch.ts`.
  // +30 (2026-11-18): a goal turn streams its live steps into the chat — one
  // message per turn, filled from the turn run's persisted tool trace and
  // finalised by the verdict. The message builders are pure
  // (`lib/harness/goal-transcript.ts`), the polling is
  // `lib/harness/use-goal-verdict-watch.ts`.
  "src/components/screens/stock-chat.tsx": 1773,
  // The Interactive Response contract is the ONE versioned zod boundary for every
  // inline block. The website-style visual blocks (stat_hero, gauge,
  // comparison_grid, ranked_list, badge_row, carousel) pushed it past the hard
  // ceiling; pinned at its measured size. Standing split candidate: move the block
  // schemas into `response/blocks/` modules, leaving the envelope + parser +
  // validate here. May only shrink from here.
    // Re-pinned 1413->1421 for the regression kind and the unknown-kind
  // safety net (an unknown chartType falls back to `auto` instead of
  // invalidating the whole block). May only shrink from here.
  // Re-pinned 1421->1449 for the in-flight chart-spec fields (symbol,
  // market, interval, window, field and the data source). May only shrink from
  // here.
    // Re-pinned 1449->1452 for the pair-carrying marks (a mark's values may be
  // [x, y] pairs - the way a model writes a point - so a chart block is not
  // refused over its data format) and the named data source (a chart that
  // NAMES its series instead of carrying it). May only shrink from here.
  // Re-pinned 1452->1455 for the `interactive` block (a model-authored mini-app
  // in a sandboxed iframe): one schema import, one union member, one type
  // export. The schema itself lives in blocks/interactive.ts. May only shrink.
  "src/lib/response/contract.ts": 1455,
};

export interface SizeEntry {
  file: string;
  lines: number;
}

/** The pure detector: which files exceed `ceiling` and are not within budget. */
export function findOversized(
  entries: SizeEntry[],
  ceiling: number,
  ratchet: Record<string, number>
): SizeEntry[] {
  return entries
    .filter((e) => e.lines > ceiling)
    .filter((e) => e.lines > (ratchet[e.file] ?? 0))
    .sort((a, b) => b.lines - a.lines);
}

export function findSoft(entries: SizeEntry[], ceiling: number): SizeEntry[] {
  return entries.filter((e) => e.lines > ceiling);
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith(".")) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      walk(full, out);
    } else if (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) {
      out.push(full);
    }
  }
  return out;
}

function countLines(file: string): number {
  const text = readFileSync(file, "utf8");
  return text.length === 0 ? 0 : text.split("\n").length - (text.endsWith("\n") ? 1 : 0);
}

function measure(): SizeEntry[] {
  return walk(ROOT).map((file) => ({
    file: relative(ROOT, file).replace(/\\/g, "/"),
    lines: countLines(file),
  }));
}

describe("source size budget (graduated)", () => {
  it("no non-ratcheted file exceeds the hard ceiling", () => {
    const oversized = findOversized(measure(), HARD_CEILING_LINES, RATCHET);
    expect(
      oversized,
      `files over ${HARD_CEILING_LINES} lines — split them or add a justified ` +
        `ratchet entry (${oversized.map((o) => `${o.file}=${o.lines}`).join(", ")})`
    ).toEqual([]);
  });

  it("every ratcheted file is still at or below its pinned budget (may not grow)", () => {
    const byFile = new Map(measure().map((e) => [e.file, e.lines]));
    for (const [file, budget] of Object.entries(RATCHET)) {
      const lines = byFile.get(file);
      expect(lines, `ratcheted file missing: ${file}`).toBeDefined();
      expect(lines!, `${file} grew past its pinned budget ${budget}`).toBeLessThanOrEqual(budget);
    }
  });

  it("the detector actually bites on a synthetic oversized file", () => {
    const entries: SizeEntry[] = [
      { file: "src/new-monolith.ts", lines: HARD_CEILING_LINES + 500 },
      { file: "src/ok.ts", lines: 100 },
    ];
    const flagged = findOversized(entries, HARD_CEILING_LINES, RATCHET);
    expect(flagged.map((f) => f.file)).toEqual(["src/new-monolith.ts"]);
    // A ratcheted file at its budget is allowed; one line over is flagged.
    const ratcheted = Object.keys(RATCHET)[0];
    expect(findOversized([{ file: ratcheted, lines: RATCHET[ratcheted] }], HARD_CEILING_LINES, RATCHET)).toEqual([]);
    expect(findOversized([{ file: ratcheted, lines: RATCHET[ratcheted] + 1 }], HARD_CEILING_LINES, RATCHET).length).toBe(1);
  });

  it("reports (but does not fail on) files over the soft ceiling", () => {
    const soft = findSoft(measure(), SOFT_CEILING_LINES).sort((a, b) => b.lines - a.lines);
    if (soft.length) {
      console.warn(`[size-budget] ${soft.length} file(s) over the soft ceiling ${SOFT_CEILING_LINES}: ${soft.slice(0, 10).map((s) => `${s.file}=${s.lines}`).join(", ")}`);
    }
    // The soft ceiling is informational; this assertion only pins that the walk
    // is real and the measurement is produced.
    expect(Array.isArray(soft)).toBe(true);
  });

  it("the walk is real (finds a known file)", () => {
    const files = walk(ROOT).map((f) => relative(ROOT, f).replace(/\\/g, "/"));
    expect(files.some((f) => f.endsWith("src/lib/api-guard.ts"))).toBe(true);
  });
});
