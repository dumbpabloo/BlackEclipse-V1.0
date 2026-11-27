'use client';

import { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { usePageShortcuts, useKeyboardShortcutsStore } from '@/hooks/use-page-shortcuts';
import dynamic from 'next/dynamic';
import { Sidebar, AppTab, Conversation } from '@/components/layout/sidebar';
import { AgentWorkspace } from '@/components/agent-workspace/workspace';
import { useTerminalShell } from '@/hooks/use-terminal-shell';
import { useTerminalStore } from '@/lib/terminal/store';
import { TerminalDock, BotSchedulerDock, ChatDock, DeferredAurora } from './lazy-chrome';
import { backTarget, recordVisit } from '@/lib/navigation-history';
import { useAppNavigation } from '@/lib/navigation/use-app-navigation';
import { TopBar } from '@/components/layout/top-bar';
import { DesktopTabBar } from '@/components/layout/desktop-tab-bar';
import { MobileBottomNav } from '@/components/layout/mobile-bottom-nav';
import { MobileDockHost } from '@/components/layout/mobile-dock-host';
import { useIsMobile } from '@/hooks/use-media-query';
import { EmptyState } from '@/components/chat/empty-state';
import { PageHeader } from '@/components/layout/page-header';
import { ChatHistory } from '@/components/chat/chat-history';
import { contentFromMessage } from '@/lib/canvas/content';
import { ChatInput, type AttachmentChip } from '@/components/chat/chat-input';
import { ApprovalCard } from '@/components/chat/approval-card';
import { ClarificationCard } from '@/components/chat/clarification-card';
import { SessionBar } from '@/components/chat/session-bar';
import { ProjectChip } from '@/components/chat/project-chip';
import { NotebookProvider } from '@/components/chat/notebook-provider';
import { ChatWordmark } from '@/components/chat/chat-wordmark';
import { useSession, mergeAttachmentChips } from '@/lib/chat/use-session';
import { useContextPicker } from '@/lib/chat/use-context-picker';
import { useProjectBinding } from '@/lib/chat/use-project-binding';
import { searchWorkspaceFiles } from '@/lib/harness/clients/code-browser';
import type { FileRef } from '@/lib/chat/file-sources';
import { buildUserTurn } from '@/lib/chat/paste';
import { DEFAULT_QUOTE_PROMPT, turnTextForHarness } from '@/lib/chat/quote';
import { useChatConversationActions } from '@/lib/chat/use-chat-conversation-actions';
import { useChatMessageActions } from '@/lib/chat/use-chat-message-actions';
import { mostRecentConversation, saveConversationsCache } from '@/lib/chat/chat-organization';
import { useChatOrganization } from '@/lib/chat/use-chat-organization';
import { useConversationPersistence } from '@/lib/chat/use-conversation-persistence';
import { useMessageQueue } from '@/lib/chat/use-message-queue';
import { useHydrateBackground } from '@/lib/backgrounds';
import { useHydrateChatAppearance } from '@/lib/chat/appearance';
import { Message } from '@/types/chat';
import { mentionedTargets } from '@/lib/bots/mentions';
import { useBotDelegation } from '@/lib/bots/use-bot-delegation';
import { useWorkspaceStore } from '@/lib/agent-workspace/store';
import { restoreWorkspaceForChat } from '@/lib/agent-workspace/restore';

// Screens — heavy ones (ECharts, WebGL bubbles, charts) are code-split and
// only fetched when their tab is opened, so the first paint compiles/loads
// just the chat shell instead of the whole app.
import type { NewsItem } from './market-news';
const MarketOverviewScreen = dynamic(
  () => import('./market-overview').then((m) => m.MarketOverviewScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const MarketBubbles = dynamic(() => import('./market-bubbles').then((m) => m.MarketBubbles), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
const MarketNews = dynamic(() => import('./market-news').then((m) => m.MarketNews), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
const ChartWidget = dynamic(() => import('@/components/chart/chart-widget').then((m) => m.ChartWidget), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
// Settings: code-split and loaded on demand. It used to be prewarmed at idle,
// but its import graph (every settings panel) is large enough that compiling it
// in the background competed with the FIRST tab's screen chunk and made that
// tab sit on the loader for tens of seconds in dev. The ScreenSkeleton covers a
// cold open; keeping this off the boot path is the better trade.
const Settings = dynamic(() => import('./settings').then((m) => m.Settings), { ssr: false, loading: () => <ScreenSkeleton /> });
import type { SectionId } from './settings';
const AlertsScreen = dynamic(() => import('@/components/screens/alerts/alerts-client').then((m) => m.AlertsClient), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
const EconomicsScreen = dynamic(() => import('./economics/economics').then((m) => m.EconomicsScreen), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});

const PortfolioScreen = dynamic(() => import('./portfolio').then((m) => m.PortfolioScreen), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
const JournalScreen = dynamic(() => import('@/components/journal/journal-screen').then((m) => m.JournalScreen), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
const DashboardScreen = dynamic(() => import('./dashboard/dashboard-screen').then((m) => m.DashboardScreen), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
const HomeDashboardScreen = dynamic(() => import('./dashboard/home-dashboard').then((m) => m.HomeDashboardScreen), {
  ssr: false,
  loading: () => <ScreenSkeleton />,
});
const MarketWorkspaceScreen = dynamic(
  () => import('@/components/market-workspace/market-workspace').then((m) => m.MarketWorkspace),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const InvestorEyeScreen = dynamic(
  () => import('@/components/investor-eye/investor-eye').then((m) => m.InvestorEyeScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const SkillsScreen = dynamic(
  () => import('@/components/screens/skills/skills-screen').then((m) => m.SkillsScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const EclipseMemoryScreen = dynamic(
  () => import('@/components/screens/eclipse-memory/eclipse-memory-screen').then((m) => m.EclipseMemoryScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const ArtifactsScreen = dynamic(
  () => import('@/components/screens/artifacts/artifacts-screen').then((m) => m.ArtifactsScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const SourcesScreen = dynamic(() => import('@/components/screens/sources/sources-screen').then((m) => m.SourcesScreen), { ssr: false, loading: () => <ScreenSkeleton /> });
const CanvasPane = dynamic(
  () => import('@/components/screens/canvas/canvas-dock').then((m) => m.CanvasDock),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const TasksScreen = dynamic(
  () => import('@/components/screens/tasks/tasks-screen').then((m) => m.TasksScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const BotsScreen = dynamic(
  () => import('@/components/screens/bots/bots-screen').then((m) => m.BotsScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const UiBuilderScreen = dynamic(
  () => import('@/components/screens/capability-studio/capability-studio-screen').then((m) => m.CapabilityStudioScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const PagesScreen = dynamic(
  () => import('@/components/screens/pages/pages-screen').then((m) => m.PagesScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
import { CodeTab } from '@/components/screens/code/code-tab';
const BacktestScreen = dynamic(
  () => import('@/components/screens/backtest/backtest-screen').then((m) => m.BacktestScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const StrategyStudioScreen = dynamic(
  () => import('@/components/screens/strategy-studio/strategy-studio-screen').then((m) => m.StrategyStudioScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const ResearchDataScreen = dynamic(
  () => import('@/components/screens/research-data/research-data-screen').then((m) => m.ResearchDataScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const FactorLabScreen = dynamic(
  () => import('@/components/screens/factors/factor-lab-screen').then((m) => m.FactorLabScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const MlLabScreen = dynamic(
  () => import('@/components/screens/ml-lab/ml-lab-screen').then((m) => m.MlLabScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const OverviewScreen = dynamic(
  () => import('@/components/screens/overview/overview-screen').then((m) => m.OverviewScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
const TablesScreen = dynamic(
  () => import('@/components/screens/tables/tables-screen').then((m) => m.TablesScreen),
  { ssr: false, loading: () => <ScreenSkeleton /> }
);
import { Newspaper, ArrowLeft } from '@/lib/icons';
import { TooltipProvider } from '@/components/ui/tooltip';
import { AttachmentPreview } from '@/components/chat/attachment-preview';
import { MarketRegimeControl } from '@/components/layout/market-regime-control';
import { useAgentRun, useRunningConversationIds, ChatRequestError } from '@/lib/agent/use-agent-run';
import { runManager } from '@/lib/agent/run-manager';
import { useChatQueueDrain } from '@/lib/chat/use-chat-queue-drain';
import { useChatModel } from '@/lib/agent/use-chat-model';
import { useReasoningEffort } from '@/lib/agent/use-reasoning-effort';
import { clientIsOperator } from '@/lib/harness/clients/skills-browser';
import { turnText, type AssistantTurn } from '@/lib/agent/response-model';
import { MarketViewsProvider } from '@/lib/dashboard/market-views';
import { useCommandStore } from '@/lib/search/store';
import { useRegimeScopeStore } from '@/lib/regime/store';
import { useSidebarKeybinds } from '@/hooks/use-sidebar-keybinds';
import { useForwardWheelScroll } from '@/hooks/use-forward-wheel-scroll';
import { sound } from '@/lib/sounds';
import { useHydrateI18n, useT } from '@/lib/i18n';
import { useThemeStore } from '@/lib/theme';
import type { Destination } from '@/lib/search/types';
import type { CommandUiAction } from '@/lib/harness/commands/command-ui';
import { useGoalWatch, type GoalTurnActivity, type GoalVerdictLine } from '@/lib/harness/use-goal-verdict-watch';
import {
  goalActivityMessage,
  goalTurnMessageId,
  goalVerdictMessage,
} from '@/lib/harness/goal-transcript';
import { destinationToQuery, parseDeepLink } from '@/lib/search/deep-link';
import { canHandleKeyboardEvent } from '@/lib/keyboard';
import { isFocusModeShortcut, isNewChatShortcut, isSettingsShortcut, isThemeShortcut } from '@/lib/shortcuts';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { TopLoader } from '@/components/ui/top-loader';
import { cn } from '@/lib/utils';

/** Apply a typed command intent through the SAME destination store the ⌘K bar uses. */
function applyCommandUi(ui: CommandUiAction | undefined): void {
  if (!ui) return;
  if (ui.type === 'openCanvas') {
    useCommandStore.getState().dispatch({
      tab: 'chat',
      ...(ui.artifactId ? { params: { artifactId: ui.artifactId } } : {}),
    });
    return;
  }
  useCommandStore.getState().dispatch({
    tab: ui.tab,
    ...(ui.subroute ? { subroute: ui.subroute } : {}),
    ...(ui.params ? { params: ui.params } : {}),
  });
}

function ScreenSkeleton() {
  // Chunk-load placeholder for next/dynamic screens. Kept minimal and
  // non-competitive with TopLoader (the single real data loader).
  return <TopLoader />;
}

function StockChatContent() {
  // Hydrate interface language + accessibility prefs from localStorage once.
  useHydrateI18n();
  // The centralized theme is hydrated once at the ROOT (`ThemeHydrator` in
  // app/layout.tsx) so standalone routes get it too — not just the shell.
  // Hydrate the app background (wallpaper; ChatBot page only).
  useHydrateBackground();
  // Hydrate the ChatBot conversation font (ChatBot page only).
  useHydrateChatAppearance();
  const t = useT();
  // Full-screen theme preview (Settings › Customization) hides the modal so the
  // real app can be auditioned; the panel stays mounted to keep its draft.
  const themePreviewMode = useThemeStore((s) => s.previewMode);
  // Boot tab (Settings › General); the URL deep link below overrides it. Applied
  // after mount so the server and the first client render stay identical.
  const [activeTab, setActiveTab] = useState<AppTab>('chat');
  const pendingCanvasArtifactId = useCommandStore((s) => s.pendingCanvasArtifactId);
  const canvasInlineOpen = useCommandStore((s) => s.canvasInlineOpen);
  const canvasView = useCommandStore((s) => s.canvasView);
  const canvasSource = useCommandStore((s) => s.canvasSource);
  const canvasOpen = canvasInlineOpen || Boolean(pendingCanvasArtifactId) || Boolean(canvasView);
  const openMessageInCanvas = useCallback((msg: { id: string; content?: string; response?: { title?: string } }) => {
    if (!msg.content) return;
    // Synchronous: the content builder is already in the chat bundle, so the
    // message body leaves the transcript the instant the button is pressed.
    const content = contentFromMessage(msg.content, { id: msg.id, title: msg.response?.title });
    useCommandStore.getState().openCanvasContent(content, {
      messageId: msg.id,
      title: content.title || 'Canvas',
    });
  }, []);
  const [query, setQuery] = useState('');
  const [selectedChartSymbol, setSelectedChartSymbol] = useState('AAPL');
  const [discoveryMarket, setDiscoveryMarket] = useState<string | undefined>(undefined);
  // Symbol pinned into the news tab by global search ("SYMBOL news").
  const [newsSymbol, setNewsSymbol] = useState<string | undefined>(undefined);
  // Symbol pre-filling the screener search ("SYMBOL screener").
  const [discoverySymbol, setDiscoverySymbol] = useState<string | undefined>(undefined);
  
  // Conversations states
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  // Bumped when a run finishes so the session bar re-lists output files.
  const [sessionRefreshKey, setSessionRefreshKey] = useState(0);
  // A delegated (@-mentioned) turn is still one-at-a-time; ordinary chat runs
  // are per conversation (see `agentBusy` below).
  const [delegationBusy, setDelegationBusy] = useState(false);
  // Prompts the user submits while a run is in flight. They drain in order as
  // soon as the conversation's current reply stops; each can be edited,
  // reordered or promoted. The queue is keyed by chat so prompts never migrate.
  const messageQueue = useMessageQueue(activeConversationId);
  // Shared session controller: the composer's "+" uploads and the bar displays.
  const sessionController = useSession(activeConversationId, sessionRefreshKey);
  const projectBinding = useProjectBinding(activeConversationId);
  const contextPicker = useContextPicker({ controller: sessionController, chatId: activeConversationId });
  // The composer container the approval card floats above (portaled to <body>).
  // Held in state (not a ref) so the card re-measures once the node mounts.
  const [composerEl, setComposerEl] = useState<HTMLDivElement | null>(null);

  // Visible attachment chips at the composer: pending + the session's saved
  // context files, de-duplicated, each carrying where it came from.
  const attachmentChips = useMemo<AttachmentChip[]>(() => {
    const pending = sessionController.pending.map((p) => ({
      name: p.name,
      origin: p.meta?.origin ?? ('upload' as const),
      sourcePath: p.meta?.sourcePath ?? null,
      rootLabel: p.meta?.rootLabel ?? null,
    }));
    const saved = (sessionController.session?.files ?? [])
      .filter((f) => f.kind === 'context')
      .map((f) => ({
        name: f.name,
        origin: f.origin ?? ('upload' as const),
        sourcePath: f.sourcePath ?? null,
        rootLabel: f.rootLabel ?? null,
      }));
    return mergeAttachmentChips(pending, saved);
  }, [sessionController.pending, sessionController.session]);

  // Project-file search for the composer's `@` palette, scoped to this chat's
  // bound folder. Errors yield an empty list (the palette still shows bots).
  const searchProjectFiles = useCallback(async (text: string): Promise<FileRef[]> => {
    const result = await searchWorkspaceFiles(text, projectBinding.rootId, 8);
    return result.files.map((file) => ({
      ...file,
      bytes: file.bytes ?? undefined,
      rootId: projectBinding.rootId,
      rootLabel: projectBinding.label,
    }));
  }, [projectBinding.rootId, projectBinding.label]);
  const reservedAttachmentNames = messages.flatMap((m) => m.attachments?.map((a) => a.name) ?? []);

  const handleRemoveAttachment = (name: string) => {
    if (sessionController.pending.some((p) => p.name === name)) {
      sessionController.removePending(name);
      return;
    }
    const file = sessionController.session?.files.find((f) => f.kind === 'context' && f.name === name);
    if (file) void sessionController.removeFile(file);
  };

  // Selects the ACTIVE conversation; background runs keep their own stream.
  const agentRun = useAgentRun({ chatId: activeConversationId ?? undefined });
  // Goal turns run as background tasks, so their live steps and verdicts are
  // streamed into the transcript by a front-door poll (the hook emits only what
  // is NEW, and only when it changed).
  const goalVerdictSink = useRef<((line: GoalVerdictLine) => void) | null>(null);
  const goalActivitySink = useRef<((snapshot: GoalTurnActivity) => void) | null>(null);
  useGoalWatch(activeConversationId, {
    onVerdict: goalVerdictSink,
    onActivity: goalActivitySink,
  });
  // Delegated (@-mentioned) turns remain a shell-level busy flag.
  const runInFlightRef = useRef(false);
  const agentBusy = agentRun.active || delegationBusy;
  // Which conversations are streaming, for the sidebar's live badges.
  const runningConversationIds = useRunningConversationIds();
  const dispatchTurnRef = useRef<
    (chatId: string, userText: string, message: Message) => Promise<void>
  >(async () => undefined);
  const chatModel = useChatModel(); // the composer's real, persisted model choice
  const reasoningEffort = useReasoningEffort(); // per-question "model intelligence"

  // The action the agent is blocked on, if any. Sourced from the live turn and
  // falling back to the last persisted assistant message, so the card survives a
  // transcript re-render mid-run (the turn sink writes it onto the message).
  const pendingApproval = useMemo(
    () => agentRun.turn.pendingApproval
      ?? [...messages].reverse().find((m) => m.role === 'assistant' && m.pendingApproval)?.pendingApproval,
    [agentRun.turn.pendingApproval, messages],
  );
  // A paused `clarify` question (not a permission decision). Same live-then-
  // persisted sourcing as the approval card.
  const pendingClarification = useMemo(
    () => agentRun.turn.pendingClarification
      ?? [...messages].reverse().find((m) => m.role === 'assistant' && m.pendingClarification)?.pendingClarification,
    [agentRun.turn.pendingClarification, messages],
  );

  // Refs mirror the two pieces of state an async send handler reads AFTER an
  // await. React state setters do not update the render closure, so reading
  // `conversations` there returns the pre-send value and a reply to a
  // just-created chat would be silently dropped. The refs are kept current
  // synchronously by every write below.
  const conversationsRef = useRef<Conversation[]>([]);
  conversationsRef.current = conversations;
  const activeConversationIdRef = useRef<string | null>(null);
  activeConversationIdRef.current = activeConversationId;
  // Mirrors the active tab for the window-level focus-mode listener below.
  const activeTabRef = useRef<AppTab>(activeTab);
  activeTabRef.current = activeTab;
  
  const [mounted, setMounted] = useState(false);
  // True once the boot deep link has been applied, so the URL-sync effect never
  // overwrites it before the tab/symbol state settles.
  const bootDoneRef = useRef(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  // Phones render the agent/scheduler docks as a bottom sheet, not a side column.
  const isMobile = useIsMobile();
  const [settingsOpen, setSettingsOpen] = useState(false);
  // True while the Overview workspace is showing the graph editor (not its
  // gallery). The app header is visible on the gallery and hidden in the editor.
  const [graphEditing, setGraphEditing] = useState(false);
  // Focus mode: a presentation-only "zen" view of the chat — hides the header
  // and sidebar and parks the transcript + composer in a centered column.
  // Scoped to the chat tab and the Ctrl+Alt+X shortcut.
  const [focusMode, setFocusMode] = useState(false);
  const [navHistory, setNavHistory] = useState<AppTab[]>(['chat']);
  const navHistoryRef = useRef(navHistory);
  navHistoryRef.current = navHistory;
  // Section requested by a `settings.<section>` palette subroute.
  const [settingsSection, setSettingsSection] = useState<SectionId | undefined>(undefined);
  // Optional block within the section, e.g. `settings.market#priority`.
  const [settingsAnchor, setSettingsAnchor] = useState<string | undefined>(undefined);

  useEffect(() => { setMounted(true); }, []);
  useAppNavigation(setActiveTab);
  // User-assigned F1–F12 shortcuts open their sidebar tab anywhere in the app.
  useSidebarKeybinds(setActiveTab, activeTab);

  // Keep-alive tabs: a tab mounts on first visit and stays mounted (hidden
  // with display:none) after that. Boot mounts ONLY the initial tab, so we
  // keep the old boot behavior (one screen fetching, no mount-all race),
  // while revisits are instant — no chunk reload, no refetch, no loader.
  const [visited, setVisited] = useState<Set<AppTab>>(() => new Set([activeTab]));
  useEffect(() => {
    setVisited(prev => (prev.has(activeTab) ? prev : new Set(prev).add(activeTab)));
  }, [activeTab]);

  useEffect(() => {
    setNavHistory((prev) => recordVisit(prev, activeTab));
  }, [activeTab]);

  // Deep-link boot + history: /?tab=portfolio or /?tab=chart&symbol=AAPL opens
  // that page directly. Powers Ctrl/Cmd-click, middle-click and "open in new
  // tab" from the sidebar, command palette and screener. Also re-applies on
  // browser back/forward so the URL stays the source of truth.
  //
  // Global command center → tab navigation. Consumes destinations dispatched
  // by the ⌘K search bar and applies them to the SPA's activeTab state machine.
  useEffect(() => {
    const apply = (dest: Destination) => {
      // `graph-studio` used to be a separate page; Overview now hosts the whole
      // workspace, so old deep links / bookmarks land on it.
      const tab: AppTab = dest.tab === 'graph-studio' ? 'overview' : dest.tab;
      if (dest.params?.action === 'new-chat') {
        handleNewChat();
      }
      // Market Regime is a top-bar panel, not a tab — open it and stop, so a
      // "market regime" search never switches the tab away.
      if (dest.params?.action === 'open-regime') {
        useRegimeScopeStore.getState().setPanelOpen(true);
        return;
      }
      if (dest.params?.symbol) {
        setSelectedChartSymbol(dest.params.symbol.toUpperCase());
        // A symbol on the news tab pins the feed to that ticker instead of
        // just loading it into the chart.
        if (dest.tab === 'news') setNewsSymbol(dest.params.symbol.toUpperCase());
        // `focus: 'chart'` is the screener pivot — pre-fill its search box.
        if (dest.tab === 'discovery' && dest.params.focus === 'chart') {
          setDiscoverySymbol(dest.params.symbol.toUpperCase());
        }
      }
      if (dest.params?.fund) {
        useCommandStore.setState({ pendingFundId: dest.params.fund });
      }
      if (dest.params?.prompt) {
        handleNewChat();
        setQuery(dest.params.prompt);
      }
      if (dest.params?.market) {
        useCommandStore.setState({ pendingMarket: dest.params.market });
        setDiscoveryMarket(dest.params.market);
      }
      if (dest.params?.artifactId) {
        useCommandStore.getState().openCanvas(dest.params.artifactId);
      }
      // Sub-view routing: `subroute` (e.g. 'economics.calendar', 'journal.analytics')
      // is published to the target screen via the command store, with an optional
      // `focus` target. Screens consume it with the `useSubroute` hook.
      if (dest.subroute) {
        const sub = dest.subroute.split('.').slice(1).join('.') || dest.subroute;
        useCommandStore.getState().requestSubroute(tab, sub, dest.params?.focus);
      }
      // Settings has no tab screen — it is a modal. Route there instead of
      // switching to a tab that renders nothing.
      if (dest.tab === 'settings') {
        const raw = dest.subroute?.split('.')[1];
        const sec = raw?.split('#')[0];
        const anchor = raw?.split('#')[1];
        if (sec) setSettingsSection(sec as SectionId);
        setSettingsAnchor(anchor);
        setSettingsOpen(true);
        return;
      }
      if (tab === 'chart') {
        // Chart needs a symbol; default to the last selected one.
        setActiveTab('chart');
      } else {
        setActiveTab(tab);
      }
      if (dest.deepLink) {
        try { window.history.pushState(null, '', dest.deepLink); } catch { /* ignore */ }
      }
      // Saved-page destinations: the keep-alive Pages screen reads the URL
      // only on mount and on `popstate`, so poke it with the same synthetic
      // event the SDK's openPage uses — palette selection then opens the
      // requested page even when the screen is already mounted.
      if (tab === 'pages' && dest.params?.page) {
        try { window.dispatchEvent(new PopStateEvent('popstate')); } catch { /* ignore */ }
      }
    };

    const check = () => {
      const dest = useCommandStore.getState().consumeDestination();
      if (dest) apply(dest);
    };

    // Boot from the URL; otherwise always land on the Eclipse Agent (chat). The
    // agent is the product's front door, so a launch never opens some other
    // screen the user left behind. An explicit deep link still wins. Applied
    // after mount so the server and first client render stay identical.
    try {
      const boot = parseDeepLink(window.location.search);
      if (boot) apply(boot);
      else setActiveTab('chat');
    } catch { /* ignore malformed URLs */ }
    bootDoneRef.current = true;

    // Browser back/forward re-applies the URL's destination.
    const onPopState = () => {
      try {
        const dest = parseDeepLink(window.location.search);
        if (dest) apply(dest);
      } catch { /* ignore malformed URLs */ }
    };
    window.addEventListener('popstate', onPopState);

    check();
    const unsub = useCommandStore.subscribe((state, prev) => {
      if (state.pendingDestination !== prev.pendingDestination && state.pendingDestination) check();
    });
    return () => {
      unsub();
      window.removeEventListener('popstate', onPopState);
    };
  }, [mounted]);

  // Keep the URL in sync with the active tab so a refresh/share opens the same
  // page and Ctrl/Cmd-click has a real href to target. `replaceState` avoids
  // polluting history on every in-app switch. When the current URL already
  // points at the same tab, its subroute/params are preserved (e.g. a settings
  // section or the chart's symbol) so the deep link survives the sync.
  useEffect(() => {
    if (!mounted || !bootDoneRef.current) return;
    try {
      const current = parseDeepLink(window.location.search);
      let next: Destination;
      if (current && current.tab === activeTab) {
        next = current;
        if (activeTab === 'chart' && selectedChartSymbol) {
          next = { tab: 'chart', params: { ...current.params, symbol: selectedChartSymbol } };
        }
      } else if (activeTab === 'chart') {
        next = { tab: 'chart', params: { symbol: selectedChartSymbol } };
      } else {
        next = { tab: activeTab };
      }
      const query = destinationToQuery(next);
      if (window.location.search.slice(1) !== query) {
        window.history.replaceState(null, '', `${window.location.pathname}?${query}`);
      }
    } catch { /* ignore malformed URLs */ }
  }, [mounted, activeTab, selectedChartSymbol]);

  // Workspace panels dispatch `be:navigate` to switch the SPA tab (e.g. the
  // Screener panel links out to the full screener tab).
  useEffect(() => {
    const onNavigate = (e: Event) => {
      const detail = (e as CustomEvent<{ tab?: string; section?: string }>).detail;
      // Settings is a modal, not a tab: open it at the requested section instead
      // of switching to a tab that renders nothing.
      if (detail?.tab === 'settings') {
        if (detail.section) setSettingsSection(detail.section as SectionId);
        setSettingsOpen(true);
        return;
      }
      if (detail?.tab) setActiveTab(detail.tab as AppTab);
    };
    window.addEventListener('be:navigate', onNavigate);
    return () => window.removeEventListener('be:navigate', onNavigate);
  }, []);

  // localStorage must never sit on the send path. Writing the whole transcript
  // list is synchronous and grows with every saved chat, so doing it inline made
  // "Enter" wait for a full JSON.stringify before the bubble could paint. The
  // in-memory store updates immediately; the cache write is coalesced and runs
  // after paint (flushed early when the page is hidden/unloaded).
  const cacheTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cachePendingRef = useRef<Conversation[] | null>(null);

  const flushConversationCache = useCallback(() => {
    if (cacheTimerRef.current !== null) {
      clearTimeout(cacheTimerRef.current);
      cacheTimerRef.current = null;
    }
    const pending = cachePendingRef.current;
    cachePendingRef.current = null;
    if (pending) saveConversationsCache(localStorage, pending);
  }, []);

  const scheduleConversationCache = useCallback((updated: Conversation[]) => {
    cachePendingRef.current = updated;
    if (cacheTimerRef.current !== null) return;
    cacheTimerRef.current = setTimeout(() => {
      cacheTimerRef.current = null;
      const pending = cachePendingRef.current;
      cachePendingRef.current = null;
      if (pending) saveConversationsCache(localStorage, pending);
    }, 400);
  }, []);

  useEffect(() => {
    const flush = () => flushConversationCache();
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') flush();
    };
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('pagehide', flush);
      document.removeEventListener('visibilitychange', onVisibility);
      flush();
    };
  }, [flushConversationCache]);

  const saveConversations = (updated: Conversation[]) => {
    conversationsRef.current = updated;
    setConversations(updated);
    // Best-effort cache. localStorage can be unavailable (private mode, a
    // sandboxed iframe, a blocked origin) — a cache write must NEVER break the
    // in-memory chat. When the list outgrows the quota the helper evicts the
    // least valuable chats instead of silently freezing on a stale snapshot.
    scheduleConversationCache(updated);
  };

  // Chat organization (folders / colors / pin / rename / move) is owned by a
  // hook; it mutates the transcript list through this bridge so the conversation
  // store stays here. The base view props are named; the org actions are bundled.
  const chatOrganization = useChatOrganization(
    (update) => saveConversations(update(conversationsRef.current)),
  );
  const loadChatOrganization = chatOrganization.load;
  // Local cache + server-transcript durability (hydration, merge, mirror) is
  // owned by the persistence hook; the shell only supplies its store + refs.
  const { activateConversation } = useConversationPersistence({
    mounted,
    conversations,
    conversationsRef,
    activeConversationIdRef,
    setConversations: (updated) => setConversations(updated),
    setActiveConversationId: (id) => setActiveConversationId(id),
    setMessages: (msgs) => setMessages(msgs),
    loadOrganization: loadChatOrganization,
  });

  // Fork + quoted-passage actions live in a hook so this at-ratchet shell does
  // not grow with the feature's orchestration.
  const conversationActions = useChatConversationActions({
    conversationsRef,
    activeConversationIdRef,
    save: saveConversations,
    setActiveConversationId: (id) => setActiveConversationId(id),
    setMessages: (messages) => setMessages(messages),
    isBusy: () => agentBusy || runInFlightRef.current,
    queueSize: () => messageQueue.sizeFor(activeConversationIdRef.current),
    onSessionCloned: () => setSessionRefreshKey((k) => k + 1),
  });
  const { pendingQuote, clearQuote, acceptQuote, forkConversation: handleForkConversation, forkFromMessage: handleForkMessage } = conversationActions;

  const handleSelectConversation = (id: string) => {
    clearQuote();
    activateConversation(id);
  };
  const handleNewChat = () => {
    activeConversationIdRef.current = null;
    setActiveConversationId(null);
    setMessages([]);
    clearQuote();
    messageQueue.clear(activeConversationIdRef.current);
    useWorkspaceStore.getState().clear();
  };

  const handleDeleteConversation = (id: string) => {
    // A live run or queued prompt would recreate a deleted conversation.
    runManager.reset(id);
    messageQueue.clear(id);
    clearQuote();
    const updated = conversationsRef.current.filter(c => c.id !== id);
    saveConversations(updated);
    if (activeConversationIdRef.current === id) {
      const next = mostRecentConversation(updated);
      if (next) {
        activateConversation(next.id);
      } else {
        activeConversationIdRef.current = null;
        setActiveConversationId(null);
        setMessages([]);
        useWorkspaceStore.getState().clear();
      }
    }
  };

  // Append to a specific conversation, or create one when the target is gone.
  const addMessageToChat = (targetId: string | null, userMsg: Message): string => {
    // Read through the refs, not the closure: two sends in the same tick (or a
    // send followed by an injected prompt) must see each other's writes.
    let currentId = targetId;
    let updatedConvs = [...conversationsRef.current];
    let becameActive = false;

    if (!currentId) {
      currentId = 'chat_' + Date.now();
      const firstLine = userMsg.content;
      const title = firstLine.slice(0, 24) + (firstLine.length > 24 ? '...' : '');
      const newChat: Conversation = {
        id: currentId,
        title,
        messages: [userMsg],
        timestamp: Date.now(),
        createdAt: Date.now()
      };
      updatedConvs = [newChat, ...updatedConvs];
      becameActive = true;
    } else {
      const idx = updatedConvs.findIndex(c => c.id === currentId);
      if (idx !== -1) {
        const chat = updatedConvs[idx];
        const newMsgList = [...chat.messages, userMsg];
        updatedConvs[idx] = {
          ...chat,
          messages: newMsgList,
          timestamp: Date.now()
        };
        const [activeChat] = updatedConvs.splice(idx, 1);
        updatedConvs = [activeChat, ...updatedConvs];
      } else {
        // The target conversation vanished (deleted, or a stale queued prompt).
        // Keep the prompt, but give it its own chat instead of dropping it.
        const wasActive = activeConversationIdRef.current === currentId;
        currentId = 'chat_' + Date.now();
        const newChat: Conversation = {
          id: currentId,
          title: userMsg.content.slice(0, 24),
          messages: [userMsg],
          timestamp: Date.now(),
          createdAt: Date.now()
        };
        updatedConvs = [newChat, ...updatedConvs];
        becameActive = wasActive;
      }
    }

    // Keep the ref in lockstep so an immediately-following append sees this.
    conversationsRef.current = updatedConvs;
    if (becameActive) {
      activeConversationIdRef.current = currentId;
      setActiveConversationId(currentId);
    }
    if (activeConversationIdRef.current === currentId) setMessages([...updatedConvs.find(c => c.id === currentId)?.messages ?? []]);
    saveConversations(updatedConvs);
    return currentId;
  };

  const addMessageToActiveChat = (userMsg: Message): string =>
    addMessageToChat(activeConversationIdRef.current, userMsg);

  const drainQueueRef = useChatQueueDrain({
    messageQueue,
    addMessageToChat,
    dispatchTurn: (chatId, userText, userMsg) =>
      dispatchTurnRef.current(chatId, userText, userMsg),
    blocked: () => runInFlightRef.current,
    conversationExists: (id) => conversationsRef.current.some((c) => c.id === id),
    revision: `${agentBusy}:${conversations.length}`,
  });

  const appendAssistantReply = (chatId: string, replyMsg: Message, persist = true) => {
    // IMPORTANT: use the ref, not `conversations`. This runs after `await`, so
    // the closure holds pre-send state and a reply to a brand-new chat would
    // otherwise be silently discarded.
    const updatedConvs = [...conversationsRef.current];
    const idx = updatedConvs.findIndex(c => c.id === chatId);
    if (idx === -1) {
      // Never drop a reply: recreate the conversation if it vanished.
      const recovered: Conversation = {
        id: chatId,
        title: replyMsg.content.slice(0, 24),
        messages: [replyMsg],
        timestamp: Date.now(),
        createdAt: Date.now(),
      };
      const next = [recovered, ...updatedConvs];
      conversationsRef.current = next;
      if (persist) saveConversations(next);
      setMessages([replyMsg]);
      return;
    }
    const chat = updatedConvs[idx];
    const newMsgList = [...chat.messages, replyMsg];
    updatedConvs[idx] = {
      ...chat,
      messages: newMsgList,
      timestamp: Date.now()
    };
    conversationsRef.current = updatedConvs;
    if (persist) saveConversations(updatedConvs);
    if (activeConversationIdRef.current === chatId) {
      setMessages(newMsgList);
    }
  };

  // A goal turn owns ONE transcript message: the activity sink fills it while
  // the turn works and the verdict sink finalises the same id, so the steps and
  // the verdict can never split into two bubbles. Ids are idempotent by
  // `goalId:turn`, so a re-render or a second poll cannot duplicate a line.
  const goalTurnMessage = (chatId: string, id: string) =>
    conversationsRef.current
      .find((conversation) => conversation.id === chatId)
      ?.messages.find((message) => message.id === id);

  const writeGoalTurn = (next: Message, persist = false) => {
    const chatId = activeConversationIdRef.current;
    if (!chatId) return;
    if (goalTurnMessage(chatId, next.id)) {
      replaceAssistantReply(chatId, next.id, next, persist);
      return;
    }
    appendAssistantReply(chatId, next, persist);
  };

  goalActivitySink.current = (snapshot: GoalTurnActivity) => {
    const chatId = activeConversationIdRef.current;
    if (!chatId) return;
    const existing = goalTurnMessage(
      chatId, goalTurnMessageId(snapshot.goalId, snapshot.turn),
    );
    // A verdict already closed this turn; a late step must not reopen it.
    if (existing?.status === 'completed') return;
    // Persist too: the step card is real work, and a reload must not erase it.
    writeGoalTurn(goalActivityMessage(snapshot, existing), true);
  };

  goalVerdictSink.current = (line: GoalVerdictLine) => {
    const chatId = activeConversationIdRef.current;
    if (!chatId) return;
    const existing = goalTurnMessage(chatId, goalTurnMessageId(line.goalId, line.turn));
    writeGoalTurn(goalVerdictMessage(line, existing), true);
  };

  /**
   * Swap an in-flight placeholder for its final message (same position).
   *
   * `persist=false` updates React state only. Streaming calls this on every
   * frame, and serializing the whole transcript to localStorage ~60×/s is the
   * single hottest write in the chat; persistence is deferred to the terminal
   * frame where it actually matters.
   */
  const replaceAssistantReply = (chatId: string, placeholderId: string, replyMsg: Message, persist = true) => {
    const updatedConvs = [...conversationsRef.current];
    const idx = updatedConvs.findIndex(c => c.id === chatId);
    if (idx === -1) {
      appendAssistantReply(chatId, replyMsg, persist);
      return;
    }
    const chat = updatedConvs[idx];
    const newMsgList = chat.messages.map(m => (m.id === placeholderId ? replyMsg : m));
    updatedConvs[idx] = { ...chat, messages: newMsgList, timestamp: Date.now() };
    conversationsRef.current = updatedConvs;
    if (persist) saveConversations(updatedConvs);
    if (activeConversationIdRef.current === chatId) {
      setMessages(newMsgList);
    }
  };

  const handleSend = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const typed = query.trim();

    // Only THIS conversation's run/queue turns a submit into a queued prompt;
    // another conversation running is irrelevant. A quote rides with it.
    if (
      agentBusy ||
      runInFlightRef.current ||
      messageQueue.sizeFor(activeConversationIdRef.current) > 0
    ) {
      const quote = pendingQuote;
      const queuedText = typed || (quote ? DEFAULT_QUOTE_PROMPT : '');
      if (!queuedText) return;
      if (messageQueue.enqueue(queuedText, quote ?? undefined)) {
        setQuery('');
        clearQuote();
        sound.tick();
      }
      return;
    }

    // A quote-only send gets a neutral prompt; the quoted passage itself is
    // assembled into the harness turn below (and shown as a card in the bubble).
    const quote = pendingQuote;
    const prompt = typed || (quote ? DEFAULT_QUOTE_PROMPT : '');
    const userMsg = buildUserTurn(prompt, sessionController.claimPastedAttachments(), attachmentChips.length > 0);
    if (!userMsg) return;
    if (quote) userMsg.quote = quote;
    sessionController.closePreview();
    setQuery('');
    clearQuote();

    const chatId = addMessageToActiveChat(userMsg);
    // Attachments made on the empty state must land in the session folder BEFORE
    // the run starts, or the run's context documents miss them. This awaits the
    // upload, so the first turn sees the attached file.
    try {
      await sessionController.flushPending(chatId);
    } catch {
      // A failed flush must not block the send; the error is surfaced on the bar.
    }
    await sessionController.flushAttachments();
    await dispatchTurn(chatId, turnTextForHarness(userMsg), userMsg);
  };

  // One harness turn per conversation; history is read from the ref post-await.
  const runAgent = async (chatId: string, userText: string, userMsg: Message) => {
    // The manager owns duplicate protection per conversation; a second run for
    // the same chat is a no-op there. Different chats may run together.
    const existing = conversationsRef.current.find((c) => c.id === chatId)?.messages ?? [userMsg];
    // Drop empty turns (cancelled/failed replies persist as ""). They carry no
    // context for the model and the front door would otherwise reject the run.
    const history: { role: 'user' | 'assistant'; content: string }[] = existing
      .slice(0, -1)
      .filter((m) => m.content.trim().length > 0 || m.quote)
      .map((m) => ({ role: m.role, content: turnTextForHarness(m) }));

    // A placeholder assistant turn; the shared run hook fills its fields.
    const placeholderId = (Date.now() + 1).toString();
    const startedAt = Date.now();
    appendAssistantReply(chatId, {
      id: placeholderId,
      role: 'assistant',
      content: '',
      status: 'queued',
      activities: [],
    });

    // Per-run sink: writes into THIS conversation even if the user switched.
    const onTurn = (turn: AssistantTurn) => {
      replaceAssistantReply(chatId, placeholderId, {
        id: placeholderId,
        role: 'assistant',
        content: turnText(turn),
        status: turn.status,
        statusNote: turn.statusNote,
        activities: turn.activities, changes: turn.changes,
        delegations: turn.delegations,
        research: turn.research, vision: turn.vision, images: turn.images,
        documents: turn.documents, memory: turn.memory, video: turn.video,
        codeVerification: turn.codeVerification, autonomy: turn.autonomy,
        capability: turn.capability, capabilityBuild: turn.capabilityBuild, analysis: turn.analysis, acquisition: turn.acquisition,
        responsePlan: turn.responsePlan,
        runId: turn.runId,
        model: turn.model,
        error: turn.error,
        pendingApproval: turn.pendingApproval,
        pendingClarification: turn.pendingClarification,
        // Elapsed turn time; finalised on the terminal frame.
        durationMs: Math.max(0, Date.now() - startedAt),
      }, turn.done);
    };

    sound.tick();

    try {
      const finalTurn = await agentRun.start({
        history,
        userText,
        chatId,
        model: chatModel.displayModel || undefined,
        reasoningEffort: reasoningEffort.effort,
        onTurn,
        onDone: () => drainQueueRef.current(),
      });
      applyCommandUi(finalTurn.ui);
      setSessionRefreshKey((k) => k + 1);
      // A finished reply is an incoming message, not a generic success.
      if (finalTurn.status === 'completed' && (finalTurn.finalText || finalTurn.text)) sound.message();
      else sound.tick();
    } catch (err) {
      // The manager records the error on the turn; ensure the message reflects
      // it even if the sink was not called.
      replaceAssistantReply(chatId, placeholderId, {
        id: placeholderId,
        role: 'assistant',
        content: '',
        status: 'error',
        error: { message: err instanceof Error ? err.message : String(err), code: err instanceof ChatRequestError ? err.code : undefined },
        durationMs: Math.max(0, Date.now() - startedAt),
      });
      sound.error();
    } finally {
      // Persist the terminal transcript once (covers cancel/abort paths too).
      saveConversations(conversationsRef.current);
    }
  };

  // Delegate `@`-mentioned turns to the real bots/rooms over the SAME runtime
  // path as the Bots workspace (no second agent loop, nothing fabricated). The
  // hook hydrates the mentionable list and owns the turn lifecycle, keeping this
  // shell small; the host hands over the transcript writers it already owns.
  const { mentionTargets, run: runDelegation, abort: abortDelegation, decide: decideDelegation } = useBotDelegation({
    model: chatModel.displayModel || undefined,
    getMessages: (id) => conversationsRef.current.find((c) => c.id === id)?.messages ?? [],
    appendAssistantReply,
    replaceAssistantReply,
    persist: () => saveConversations(conversationsRef.current),
    setBusy: setDelegationBusy,
    isInFlight: () => runInFlightRef.current,
    setInFlight: (v) => { runInFlightRef.current = v; },
    runFallback: (id, text) => runAgent(id, text, { id: Date.now().toString(), role: 'user', content: text }),
  });

  /**
   * Route a user turn: to the mentioned bot(s)/room(s) when the message contains
   * a resolvable `@mention`, otherwise to the normal Eclipse agent. Every send
   * path funnels through here so mentions work everywhere (send, follow-up,
   * edit/redirect, injected prompts).
   */
  const dispatchTurn = async (chatId: string, userText: string, userMsg: Message) => {
    const targets = mentionedTargets(userText, mentionTargets);
    if (targets.length > 0) await runDelegation(chatId, userText, targets);
    else await runAgent(chatId, userText, userMsg);
  };

  // The queue-drain hook holds a ref to the latest dispatcher; keep it current.
  dispatchTurnRef.current = dispatchTurn;

  /** Stop the in-flight run (cooperative cancel in the Python runtime). */
  const handleCancel = async () => {
    sound.tick();
    // Abort a delegated (@-mentioned) turn if one is in flight; otherwise the
    // single main-agent run. Aborting stops the client reading the stream and
    // keeps whatever already streamed (same contract as the Bots workspace Stop).
    if (abortDelegation()) return;
    agentRun.cancel();
  };

  /**
   * Promote a queued prompt to the front and stop the current run, so it is the
   * very next thing the agent does. This is the "override/re-steer" action: the
   * queue keeps everything else, and the drain effect starts the promoted item.
   */
  const handleQueueRunNow = (id: string) => {
    messageQueue.promote(id);
    if (agentBusy || runInFlightRef.current) void handleCancel();
  };

  // Feedback / retry / edit-branch / redirect are transcript mutations; they
  // live in a hook so this at-ratchet shell does not carry the boilerplate.
  const messageActions = useChatMessageActions({
    conversationsRef,
    activeConversationIdRef,
    save: saveConversations,
    setMessages: (messages) => setMessages(messages),
    addUserMessage: addMessageToActiveChat,
    dispatch: dispatchTurn,
  });
  const { feedback: handleFeedback, retry: handleRetryMessage, edit: handleEditMessage, resend: handleResendMessage } = messageActions;

  /** Inject a prompt and run it through the Eclipse Agent (the harness) so the
   *  answer is grounded in JDdS tools — never a canned fabricated reply. */
  const runAgentPrompt = async (chatId: string, userText: string) => {
    const userMsg: Message = { id: Date.now().toString(), role: 'user', content: userText };
    await dispatchTurn(chatId, userText, userMsg);
  };

  /** Run a follow-up turn from a response `controls` block (same path as send). */
  const handleFollowup = (prompt: string) => {
    const text = prompt.trim();
    if (!text || agentBusy) return;
    const userMsg: Message = { id: Date.now().toString(), role: 'user', content: text };
    const chatId = addMessageToActiveChat(userMsg);
    void dispatchTurn(chatId, text, userMsg);
  };

  const handleInjectPrompt = (ticker: string, promptType: 'research' | 'discuss', customContext?: string) => {
    setActiveTab('chat');

    const customPrompt = promptType === 'research'
      ? `Generate a comprehensive AI research report on ${ticker} detailing financial health, P/E ratios, and growth vectors.`
      : `What is the market sentiment and outlook on ${ticker} given recent institutional activity?`;

    const userMsg: Message = { id: Date.now().toString(), role: 'user', content: customPrompt };
    const chatId = addMessageToActiveChat(userMsg);

    if (customContext) {
      // Caller-provided context (e.g. screener row data) is shown verbatim as
      // data context, then real research runs on top of it.
      const ctxMsg: Message = {
        id: (Date.now() + 3).toString(),
        role: 'assistant',
        content: `Data context for "${ticker}":\n\n${customContext}`,
      };
      appendAssistantReply(chatId, ctxMsg);
    }
    void runAgentPrompt(chatId, customContext ? `${customPrompt}\n\nContext data: ${customContext}` : customPrompt);

    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const handleDiscussNews = (news: NewsItem) => {
    setActiveTab('chat');
    const prompt = `Discuss the market implications of: "${news.headline}" for ${news.ticker || 'the market'}.`;

    const userMsg: Message = { id: Date.now().toString(), role: 'user', content: prompt };
    const chatId = addMessageToActiveChat(userMsg);
    void runAgentPrompt(chatId, prompt);

    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  // The Market Regime panel dispatches `be:ask-eclipse` with a grounded prompt
  // (built from the live regime read). Route it through the same agent path so
  // the explanation is tool-backed, never a canned reply.
  useEffect(() => {
    const onAsk = (e: Event) => {
      const prompt = (e as CustomEvent<{ prompt?: string }>).detail?.prompt;
      if (!prompt) return;
      setActiveTab('chat');
      const userMsg: Message = { id: Date.now().toString(), role: 'user', content: prompt };
      const chatId = addMessageToActiveChat(userMsg);
      void runAgentPrompt(chatId, prompt);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    };
    window.addEventListener('be:ask-eclipse', onAsk);
    return () => window.removeEventListener('be:ask-eclipse', onAsk);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const chatRootRef = useRef<HTMLDivElement>(null);
  const appRootRef = useRef<HTMLDivElement>(null);
  // The transcript's scroll element, supplied to ChatHistory so the wrapper can
  // forward wheel/trackpad scrolling from the composer/toolbar area (DOM siblings
  // of the transcript) back onto it — otherwise resting the cursor on the
  // composer and scrolling does nothing on the ChatBot page.
  const chatScrollRef = useRef<HTMLDivElement>(null);
  // Hydrate the terminal's persisted state and tuck it away in focus mode.
  useTerminalShell(focusMode);
  const terminalOpen = useTerminalStore((s) => s.open);

  // Focus mode is chat-only: leave it the moment another tab is opened, and
  // never keep it on while the settings modal is up.
  useEffect(() => {
    if (focusMode && activeTab !== 'chat') setFocusMode(false);
  }, [focusMode, activeTab]);
  useEffect(() => {
    if (focusMode && settingsOpen) setFocusMode(false);
  }, [focusMode, settingsOpen]);
  // Ctrl+Alt+X toggles focus mode in the app shell (chat tab only); skip text
  // fields so AltGr's physical KeyX (`ż`) cannot fire mid-word.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!isFocusModeShortcut(event) || !canHandleKeyboardEvent(event)) return;
      if (useKeyboardShortcutsStore.getState().helpOpen) return;
      if (activeTabRef.current !== 'chat') return;
      event.preventDefault();
      setFocusMode((v) => !v);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);
  // Ctrl/Cmd+Alt+N starts a new chat from ANY tab: switch to the ChatBot page,
  // clear the transcript, then drop the caret in the composer via `be:focus-chat`.
  // Ctrl+N is NOT usable — browsers reserve it and ignore `preventDefault`.
  // It also matches AltGr's physical KeyN, so skip text fields while typing.
  const newChatRef = useRef(handleNewChat);
  newChatRef.current = handleNewChat;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!isNewChatShortcut(event) || !canHandleKeyboardEvent(event)) return;
      if (useKeyboardShortcutsStore.getState().helpOpen) return;
      event.preventDefault();
      setActiveTab('chat');
      newChatRef.current();
      // The composer mounts on the next paint; focus it once it is on screen.
      setTimeout(() => window.dispatchEvent(new CustomEvent('be:focus-chat')), 0);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);
  // Ctrl/Cmd+, opens Settings; Ctrl/Cmd+Shift+L flips the colour scheme. Both
  // stay out of text fields and other blocking dialogs like every page chord.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!canHandleKeyboardEvent(event) || useKeyboardShortcutsStore.getState().helpOpen) return;
      if (isSettingsShortcut(event)) {
        event.preventDefault();
        setSettingsSection(undefined);
        setSettingsAnchor(undefined);
        setSettingsOpen(true);
        return;
      }
      if (isThemeShortcut(event)) {
        event.preventDefault();
        const theme = useThemeStore.getState().theme;
        useThemeStore.getState().setTheme({ scheme: theme.scheme === 'light' ? 'dark' : 'light' });
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);
  const handleBack = () => {
    if (settingsOpen || mobileMenuOpen) return;
    const target = backTarget(navHistoryRef.current);
    if (!target) return;
    navHistoryRef.current = target.history;
    setNavHistory(target.history);
    setActiveTab(target.tab);
  };
  usePageShortcuts(appRootRef, [
    { id: 'nav.back', label: 'Back', keys: 'Alt+ArrowLeft', run: handleBack, enabled: navHistory.length > 1 && !settingsOpen && !mobileMenuOpen },
  ]);

  const handleChartTicker = (symbol?: string) => {
    if (symbol) {
      setSelectedChartSymbol(symbol);
    }
    setActiveTab('chart');
  };

  // Forward wheel/trackpad scrolling from anywhere on the ChatBot page (header,
  // composer, padding) onto the transcript — they are DOM siblings of the
  // transcript, so the browser cannot chain their wheel events to it. Only the
  // active chat tab with a transcript needs it.
  useForwardWheelScroll(chatScrollRef, activeTab === 'chat' && messages.length > 0);

  // ── Aurora glow ─────────────────────────────────────────────────────────
  // The app-wide market-state indicator is mounted via <AuroraGlow>, which owns
  // the sentiment subscription + user palette and renders the masked top glow.
  // Fullscreen mounts its own copy (it portals to <body>, above the shell).

  return (
    <TooltipProvider delay={0}>
    <div ref={appRootRef} className="app-page-bg relative flex flex-col h-[100dvh] w-full bg-background text-foreground font-sans selection:bg-primary/30 overflow-hidden">
      {/* Keyboard users can jump straight past the sidebar to the main content. */}
      <a href="#main-content" className="skip-to-content">
        {t('a11y.skipToContent')}
      </a>
      {/* Aurora full-viewport top glow — reflects blended market + news sentiment */}
      <DeferredAurora />
      {/* Market Regime panel — headless; opened from the global search / shell.
          Not a header control. */}
      <MarketRegimeControl />

      {/* The primary row: sidebar | main column | optional right-docked terminal.
          The terminal is a REAL column here, so the main panel shrinks to make
          room instead of being overlapped. */}
      <div className="flex-1 flex flex-col md:flex-row min-h-0 overflow-hidden">
        <Sidebar 
          activeTab={activeTab} 
          onChangeTab={setActiveTab} 
          conversations={conversations}
          organization={chatOrganization}
          activeConversationId={activeConversationId}
          onSelectConversation={handleSelectConversation}
          onNewChat={handleNewChat}
          onForkConversation={handleForkConversation}
          onDeleteConversation={handleDeleteConversation}
          onOpenSettings={() => setSettingsOpen(true)}
          mobileOpen={mobileMenuOpen}
          onCloseMobile={() => setMobileMenuOpen(false)}
          hidden={focusMode}
          runningConversationIds={runningConversationIds}
          queuedCounts={messageQueue.counts}
        />

        <div
          className={cn(
            'flex-1 flex flex-col relative z-10 overflow-hidden min-w-0 h-full',
            activeTab !== 'chat' &&
              'glass-card before:pointer-events-none before:absolute before:inset-x-0 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-white/60 dark:before:via-white/10 before:to-transparent',
          )}
        >
        <DesktopTabBar activeTab={activeTab} onSelectTab={setActiveTab} onNewChat={handleNewChat} conversationId={activeConversationId} conversations={conversations} onSelectConversation={handleSelectConversation} />
        {/* ChatBot wallpaper. Sits in the main content panel (right of the
            sidebar) so it resizes with the panel and stays fully visible when
            the sidebar opens/closes. Rendered only on the chat landing (empty
            transcript); the vars come from the background store and paint
            `none` unless an image is set. */}
        {activeTab === 'chat' && messages.length === 0 && (
          <div aria-hidden className="app-bg-layer" />
        )}

        {/* Chat-conversation surface (Settings › ChatBot Page › Chat background).
            On by default: paints a calm #0a0b0b (dark) / matte off-white (light)
            behind the transcript + composer, over the wallpaper, so messages stay
            readable. Rendered only once a conversation exists, so the landing
            wallpaper/wordmark still show. No-op (opacity 0) if disabled. */}
        {activeTab === 'chat' && messages.length > 0 && <div aria-hidden className="chat-surface-layer" />}

        {/* Aurora on the ChatBot landing. The shell's AuroraGlow (z-0) is covered
            by the chat panel's opaque wallpaper, so mount a local copy above it,
            below the wordmark/composer, on the LANDING only. Pinned to the
            VIEWPORT (`fixed`) so it stays full-width/centered as the sidebar or
            dock resizes the panel — panel z-10 keeps it behind both. Mirrors the
            fullscreen Command Center overlay. */}
        {activeTab === 'chat' && messages.length === 0 && <DeferredAurora className="fixed" />}

        {/* Giant ChatBot wordmark (Settings › General › Background). Painted
            above the backdrop but below the transcript/composer, landing only. */}
        {activeTab === 'chat' && messages.length === 0 && <ChatWordmark />}

        <TopBar 
          onToggleMenu={() => setMobileMenuOpen(true)}
          activeTab={activeTab}
          // Overview keeps the app header; its in-place editor goes full-bleed.
          hidden={focusMode || (activeTab === 'overview' && graphEditing)}
        />

        <div id="main-content" className="flex-1 overflow-y-auto relative w-full h-full z-10 pb-[4.25rem] md:pb-0" tabIndex={-1}>
          {/* Keep-alive tabs: a tab mounts on first visit (visited set) and
              then stays mounted hidden — revisits are instant with no chunk
              reload, no refetch, no loader flash. Unvisited tabs render
              nothing, preserving the old single-screen boot behavior. */}
          {visited.has('home') && (
            <div data-keepalive-panel className={cn('w-full h-full', activeTab === 'home' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-2 md:px-4 py-3">
                <HomeDashboardScreen active={activeTab === 'home'} />
              </div>
            </div>
          )}

          {visited.has('dashboard') && (
            <div data-keepalive-panel className={cn('w-full h-full', activeTab === 'dashboard' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-2 md:px-4 py-3">
                <MarketViewsProvider>
                  <DashboardScreen active={activeTab === 'dashboard'} />
                </MarketViewsProvider>
              </div>
            </div>
          )}

          {visited.has('market-workspace') && (
            <div className={cn('w-full', activeTab === 'market-workspace' ? '' : 'hidden')}>
              <div className="w-full px-3 md:px-5 py-4">
                <MarketWorkspaceScreen />
              </div>
            </div>
          )}

          {visited.has('investor-eye') && (
            <div className={cn('w-full h-full', activeTab === 'investor-eye' ? '' : 'hidden')}>
              <InvestorEyeScreen />
            </div>
          )}

          {visited.has('chat') && (
            <div
              ref={chatRootRef}
              data-keyboard-scope="Chat"
              className={activeTab === 'chat' ? 'w-full h-full' : 'hidden'}
            >
              <NotebookProvider chatId={activeConversationId}>
              <div className={cn(
                'w-full h-full flex flex-col items-center justify-between pt-2 md:pt-4 relative min-w-0 transition-[padding] duration-500 ease-out',
                focusMode && 'pt-0',
              )}
                style={{ fontFamily: 'var(--chat-font, var(--font-sans))' }}
              >
                {messages.length === 0 ? (
                  <div className="flex-1 flex w-full flex-col items-center min-h-0">
                    <EmptyState
                      query={query}
                      setQuery={setQuery}
                      onSend={handleSend}
                      onRunShell={messageActions.runShell}
                      onViewMarketOverview={() => setActiveTab('discovery')}
                      onAttach={contextPicker.open}
                      onAttachImage={contextPicker.openImages}
                      attachments={attachmentChips} onRemoveAttachment={handleRemoveAttachment} onOpenAttachment={sessionController.openAttachment}
                      onPasteText={(text) => void sessionController.attachText(text, reservedAttachmentNames)}
                      busy={agentBusy}
                      onCancel={handleCancel}
                      modelState={chatModel}
                      effortState={reasoningEffort}
                      mentionTargets={mentionTargets}
                      searchProjectFiles={searchProjectFiles}
                      onReferenceFile={sessionController.attachProjectFile}
                    />
                    {/* Same px + max-w as the composer column, so it can't overflow. */}
                    <div className="w-full px-4 md:px-6">
                      <div className="mx-auto flex w-full min-w-0 max-w-full items-center gap-2 md:max-w-[80%]">
                        <SessionBar session={sessionController} className="min-w-0 flex-1" /><ProjectChip chatId={activeConversationId ?? undefined} />
                      </div>
                    </div>
                  </div>
                ) : (
                  <div
                    className={cn(
                    'flex-1 flex flex-col items-center w-full min-h-0 relative z-10 overflow-hidden justify-between transition-[max-width] duration-500 ease-out',
                    focusMode && 'max-w-none mx-auto',
                  )}>
<ChatHistory
  messages={messages}
  onEditMessage={handleEditMessage}
  onResendMessage={handleResendMessage}
  onRetryMessage={handleRetryMessage}
  onOpenAttachment={sessionController.openAttachment}
  onFeedback={handleFeedback}
  onQuote={acceptQuote}
  onForkMessage={handleForkMessage}
  onFollowup={handleFollowup}
  onOpenCanvas={openMessageInCanvas}
  canvasMessageId={focusMode ? null : canvasSource?.messageId ?? null}
  scrollRef={chatScrollRef}
  bottomInset={132} />
                    <div className={cn(
                      'absolute inset-x-0 bottom-0 z-30 w-full mx-auto px-4 md:px-6 group pb-2 max-w-full md:max-w-[80%]',
                    )} ref={setComposerEl}>
                      <ChatInput
                        query={query}
                        setQuery={setQuery}
                        onSend={handleSend}
                        onRunShell={messageActions.runShell}
                        placeholder="Ask a follow-up..."
                        showProvider={false}
                        modelState={chatModel}
                        effortState={reasoningEffort}
                        busy={agentBusy}
                        onCancel={handleCancel}
                        onAttach={contextPicker.open}
                        onAttachImage={contextPicker.openImages}
                        attaching={sessionController.busy}
                        attachments={attachmentChips} onRemoveAttachment={handleRemoveAttachment} onOpenAttachment={sessionController.openAttachment}
                        quote={pendingQuote}
                        onClearQuote={clearQuote}
                        onPasteText={(text) => void sessionController.attachText(text, reservedAttachmentNames)}
                        mentionTargets={mentionTargets}
                        searchProjectFiles={searchProjectFiles}
                        onReferenceFile={sessionController.attachProjectFile}
                        queued={messageQueue.items.filter((item) => (item.chatId ?? null) === activeConversationId)}
                        onQueueEdit={messageQueue.update}
                        onQueueRemove={messageQueue.remove}
                        onQueueMove={messageQueue.move}
                        onQueueRunNow={handleQueueRunNow}
                        onQueueClear={() => messageQueue.clear(activeConversationId)}
                        contextLabel={sessionController.session && sessionController.session.contextChars > 0 ? `${(sessionController.session.contextChars / 1000).toFixed(1)}k / ${(sessionController.session.contextLimitChars / 1000).toFixed(0)}k chars` : undefined}
                        contextFraction={sessionController.session ? sessionController.session.contextChars / sessionController.session.contextLimitChars : 0}
                      />
                      {pendingApproval && (
                        <ApprovalCard
                          approval={pendingApproval}
                          anchor={composerEl}
                          // A card from the main run decides via `agentRun`; one from a
                          // delegated (@-mentioned) run decides via the delegation hook.
                          onDecide={(approve, remember) => {
                            void (agentRun.turn.pendingApproval
                              ? agentRun.decide(approve, remember)
                              : decideDelegation(approve, remember, pendingApproval.id));
                          }}
                        />
                      )}
                      {pendingClarification && (
                        <ClarificationCard
                          clarification={pendingClarification}
                          anchor={composerEl}
                          onAnswer={(value) => agentRun.answer(value)}
                        />
                      )}
                      <AttachmentPreview preview={sessionController.preview} onClose={sessionController.closePreview} />
                      <div className="mt-1.5 flex min-w-0 items-center gap-2">
                        <SessionBar session={sessionController} className="min-w-0 flex-1" />
                        <ProjectChip chatId={activeConversationId ?? undefined} compact />
                      </div>
                    </div>
                  </div>
                )}
                {contextPicker.host}
              </div>
              </NotebookProvider>
            </div>
          )}

          {visited.has('news') && (
            <div className={cn('w-full h-full', activeTab === 'news' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto h-full pb-20">
                <div className="animate-fade-in-up w-full h-full flex flex-col">
                  <PageHeader
                    icon={Newspaper}
                    title="Market News & Bulletins"
                    description="Live updates, professional ratings indicators, and direct discuss-to-chat triggers."
                    size="lg"
                    className="mb-6 pb-3 sm:flex-row"
                  />
                  <MarketNews
                    onDiscussNews={handleDiscussNews}
                    onAnalyzeTicker={handleChartTicker}
                    fullPage={true}
                    symbol={newsSymbol}
                    active={activeTab === 'news'}
                  />
                </div>
              </div>
            </div>
          )}

          {visited.has('discovery') && (
            <div className={cn('w-full h-full', activeTab === 'discovery' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8">
                <div className="animate-fade-in-up w-full h-full">
                  <MarketOverviewScreen
                    key={`${discoveryMarket ?? 'default'}:${discoverySymbol ?? ''}`}
                    market={discoveryMarket}
                    initialSymbol={discoverySymbol}
                    onAnalyzeTicker={(t: string, customCtx?: string) => handleInjectPrompt(t, 'research', customCtx)}
                    onSelectChartSymbol={(sym) => {
                      setSelectedChartSymbol(sym);
                      setActiveTab('chart');
                    }}
                    embedded={false}
                  />
                </div>
              </div>
            </div>
          )}

          {visited.has('heatmap') && (
            <div className={cn('w-full h-full', activeTab === 'heatmap' ? '' : 'hidden')}>
              <MarketBubbles active={activeTab === 'heatmap'} onAnalyzeTicker={t => handleInjectPrompt(t, 'research')} />
            </div>
          )}


          {visited.has('alerts') && (
            <div className={cn('w-full h-full', activeTab === 'alerts' ? '' : 'hidden')}>
              <div className="flex-1 overflow-y-auto">
                <AlertsScreen active={activeTab === 'alerts'} />
              </div>
            </div>
          )}

          {visited.has('economics') && (
            <div className={cn('w-full h-full', activeTab === 'economics' ? '' : 'hidden')}>
              <EconomicsScreen />
            </div>
          )}

          {visited.has('skills') && (
            <div className={cn('w-full h-full', activeTab === 'skills' ? '' : 'hidden')}>
              <SkillsScreen isOperator={clientIsOperator()} />
            </div>
          )}

          {visited.has('memory') && (
            <div className={cn('w-full h-full', activeTab === 'memory' ? '' : 'hidden')}>
              <EclipseMemoryScreen isOperator={clientIsOperator()} />
            </div>
          )}

          {visited.has('artifacts') && (
            <div className={cn('w-full h-full', activeTab === 'artifacts' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto">
                <ArtifactsScreen />
              </div>
            </div>
          )}

          {visited.has('sources') && (
            <div className={cn('w-full h-full', activeTab === 'sources' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto">
                <SourcesScreen active={activeTab === 'sources'} />
              </div>
            </div>
          )}

          {visited.has('tasks') && (
            <div className={cn('w-full h-full', activeTab === 'tasks' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto">
                <TasksScreen active={activeTab === 'tasks'} />
              </div>
            </div>
          )}

          {visited.has('code') && <CodeTab active={activeTab === 'code'} />}

          {visited.has('bots') && (
            <div className={cn('w-full h-full', activeTab === 'bots' ? '' : 'hidden')}>
              <BotsScreen active={activeTab === 'bots'} />
            </div>
          )}

          {visited.has('ui-builder') && (
            <div className={cn('w-full h-full', activeTab === 'ui-builder' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto">
                <UiBuilderScreen />
              </div>
            </div>
          )}

          {visited.has('pages') && (
            <div className={cn('w-full h-full', activeTab === 'pages' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8">
                <PagesScreen />
              </div>
            </div>
          )}

          {visited.has('backtest') && (
            <div className={cn('w-full h-full', activeTab === 'backtest' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto">
                <BacktestScreen />
              </div>
            </div>
          )}

          {visited.has('strategy-studio') && (
            <div className={cn('w-full h-full', activeTab === 'strategy-studio' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto">
                <StrategyStudioScreen />
              </div>
            </div>
          )}

          {visited.has('research-data') && (
            <div className={cn('w-full h-full', activeTab === 'research-data' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8 overflow-y-auto">
                <ResearchDataScreen />
              </div>
            </div>
          )}

          {visited.has('factor-lab') && (
            <div className={cn('w-full h-full', activeTab === 'factor-lab' ? '' : 'hidden')}>
              <FactorLabScreen />
            </div>
          )}

          {visited.has('ml-lab') && (
            <div className={cn('w-full h-full', activeTab === 'ml-lab' ? '' : 'hidden')}>
              <MlLabScreen />
            </div>
          )}

          {visited.has('overview') && (
            <div className={cn('w-full h-full', activeTab === 'overview' ? '' : 'hidden')}>
              <OverviewScreen onEditorChange={setGraphEditing} />
            </div>
          )}

          {visited.has('tables') && (
            <div className={cn('w-full h-full', activeTab === 'tables' ? '' : 'hidden')}>
              <TablesScreen
                onOpenDataSources={() => {
                  setSettingsSection('market');
                  setSettingsOpen(true);
                }}
              />
            </div>
          )}

          {visited.has('portfolio') && (
            <div className={cn('w-full h-full', activeTab === 'portfolio' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8">
                <PortfolioScreen />
              </div>
            </div>
          )}

          {visited.has('journal') && (
            <div className={cn('w-full h-full', activeTab === 'journal' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-8">
                <JournalScreen />
              </div>
            </div>
          )}

          {visited.has('chart') && (
            <div className={cn('w-full h-full', activeTab === 'chart' ? '' : 'hidden')}>
              <div className="flex-1 w-full px-4 md:px-8 py-6 h-full">
                <button
                  onClick={() => setActiveTab('discovery')}
                  className="mb-3 inline-flex items-center gap-1.5 text-xs font-semibold text-muted-foreground hover:text-primary border border-border/40 hover:border-primary/40 bg-accent/30 hover:bg-accent/60 rounded-xl px-3 py-1.5 transition-all cursor-pointer"
                >
                  <ArrowLeft size={13} /> Back to Market Discover
                </button>
                <ChartWidget symbol={selectedChartSymbol} className="h-full" active={activeTab === 'chart'} />
              </div>
            </div>
          )}

          {/* App-wide floating assistant — present on every tab except the
              ChatBot and Bots (each already has its own composer, so the dock
              would be a second, competing input) and while the Settings modal
              is open. Mounted once here rather than per-screen; the dock gates
              on this panel's own display so it never floats over an inactive
              keep-alive tab. */}
          {activeTab !== 'chat' && activeTab !== 'bots' && activeTab !== 'tables' && activeTab !== 'overview' && !settingsOpen && (
            <ChatDock visible />
          )}
        </div>
        {/* Close the main column, then place the right dock as a SIBLING column
            in the row, so the main panel shrinks instead of being overlapped. */}
        </div>
        {/* The right-docked column: the execution Agent Workspace everywhere,
            except Bots (jobs scheduler). Hidden in focus mode for a full page. */}
        {!focusMode && (canvasOpen
          ? <CanvasPane />
          : isMobile
            ? <MobileDockHost activeTab={activeTab} />
            : (activeTab === 'bots'
              ? <BotSchedulerDock />
                : activeTab === 'tables'
                  ? null // Tables is a full-width data surface.
                  : activeTab === 'overview'
                    ? null // Overview owns the Graph Studio inspector/run rail.
                    : <AgentWorkspace showExplain={activeTab === 'chat'} />))}
        {terminalOpen && <TerminalDock slot="right" />}
      </div>

      {/* Bottom-docked terminal: a real strip below the row, above the footer. */}
      {terminalOpen && <TerminalDock slot="bottom" />}
      {/* Full-page terminal: an overlay, since nothing else is visible behind it. */}
      {terminalOpen && <TerminalDock slot="full" />}

      {/* Phone-only primary navigation: fixed bottom bar + "More" sheet. */}
      <MobileBottomNav
        activeTab={activeTab}
        onChangeTab={setActiveTab}
        hidden={focusMode || settingsOpen}
      />

      <Dialog
        open={settingsOpen}
        // While previewing, the floating bar owns exit/apply/discard; ignore any
        // close request (Esc/backdrop) so the mounted panel keeps its draft.
        onOpenChange={(o) => { if (!o && themePreviewMode) return; setSettingsOpen(o); }}
        modal={!themePreviewMode}
      >
        <DialogContent
          overlayClassName={cn(themePreviewMode && 'hidden')}
          className={cn(
            'w-[calc(100%-2rem)] max-w-6xl sm:max-w-6xl h-[min(90dvh,880px)] p-0 gap-0 overflow-hidden rounded-2xl border border-white/10 glass-card-strong shadow-2xl text-foreground',
            // Full-screen preview: keep the panel MOUNTED (draft state survives)
            // but hidden, so the real app is visible behind the floating bar.
            themePreviewMode && 'hidden',
          )}
        >
          <DialogTitle className="sr-only">{t('common.settings')}</DialogTitle>
          {/* Keyed so a palette `settings.<section>` request remounts into the
              requested section even while the modal is already open. */}
          <Settings
            key={`${settingsSection ?? 'default'}:${settingsAnchor ?? ''}`}
            initialSection={settingsSection}
            initialAnchor={settingsAnchor}
          />
        </DialogContent>
      </Dialog>
    </div>
    </TooltipProvider>
  );
}

export function StockChat() {
  return <StockChatContent />;
}
