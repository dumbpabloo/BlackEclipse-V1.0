import type {Metadata} from 'next';
import './globals.css';
import './fonts.css';
import './katex.css';
import { cn } from "@/lib/utils";
import { AgentationMount } from "@/components/layout/agentation-mount";
import { ThemeHydrator } from "@/components/layout/theme-hydrator";
import { GlobalLoader } from "@/components/ui/global-loader";
import { AppActivity } from "@/components/layout/app-activity";
import { OnboardingGate } from "@/components/setup/onboarding-gate";
import { onboardingGateState } from "@/lib/setup/gate";
import { RiskAckGate } from "@/components/legal/risk-ack-gate";
import { GlobalSearchMount } from "@/components/layout/global-search-mount";
import { KeyboardShortcuts } from "@/components/layout/keyboard-shortcuts";
import { AlertNotificationProvider } from "@/components/layout/alert-notification-provider";
import { ConfirmDialogHost } from "@/components/ui/confirm-dialog-host";
import { TaskCompletionWatcher } from "@/lib/notifications/task-watcher";
import { BotsRunWatcher } from "@/lib/notifications/bots-run-watcher";
import { ChannelActivityWatcher } from "@/lib/notifications/channel-watcher";
import { NewsAlertWatcher } from "@/lib/notifications/news-watcher";
import { BrowserTabIndicator } from "@/lib/notifications/tab-indicator";
import { DesktopTaskbarBadge } from "@/lib/notifications/desktop-taskbar-badge";
import { UpdateBanner } from "@/components/layout/update-banner";
import { OfflineBanner } from "@/components/layout/offline-banner";
import { DesktopSession } from "@/components/layout/desktop-session";
import { DesktopShellGuards } from "@/components/layout/desktop-shell-guards";
import { SoundProvider } from "@/lib/sounds";
import { TtsProvider } from "@/lib/tts";
import { TerminalKeybind } from "@/components/layout/terminal-keybind";
import { WorkspaceKeybind } from "@/components/agent-workspace/workspace-keybind";

// ── Landing-wordmark fonts ──────────────────────────────────────────────────
// Real display faces for the ChatBot wordmark (Settings › General › Background),
// exposed as CSS variables the wordmark options reference. Curated for range:
// elegant serif, sci-fi display, pixel, ASCII/mono and blackletter. Keep this
// list in sync with WORDMARK_FONT_OPTIONS in src/lib/theme/tokens.ts.
//
// They are SELF-HOSTED (public/fonts/ + src/app/fonts.css), not fetched from
// Google at build time: a live fetch makes the build depend on the network and
// on Google answering with exactly one font entry per query. When it did not,
// Turbopack could not resolve the import and the WHOLE app failed to compile.

export const metadata: Metadata = {
  title: 'BlackEclipse — AI Stock Research & Screening Platform',
  description: 'Advanced glassmorphic stock screening tables, live sectors treemap heatmap, portfolio watchlist, and financial news, backed by an autonomous AI research analyst.',
  icons: { icon: '/branding/FAVICON.svg' },
};

export default async function RootLayout({children}: {children: React.ReactNode}) {
  // Server-side, per request: withhold every app-wide poller/stream/update
  // check while a first-run wizard owns the screen (see AppActivity). Doing it
  // here rather than from a client flag keeps SSR and hydration identical.
  const setupPending = (await onboardingGateState()) === "incomplete";

  return (
    <html lang="en" suppressHydrationWarning className={cn("dark", "font-sans")}>
      <body suppressHydrationWarning>
        {/* Pre-paint background: without it, a reload paints the WebView's
         * default white until globals.css arrives (very visible in the glass
         * window). The theme script below flips `dark` first, and the glass
         * class makes this transparent once the shell applies acrylic. */}
        <style
          dangerouslySetInnerHTML={{
            __html: `html{background:#0a0b0b}html:not(.dark){background:#f5f5f7}html.glass-window{background:transparent}`,
          }}
        />
        {/* Pre-paint scheme sync. The default document is `dark` (matches the
         * shipped Eclipse theme, no flash). If the user saved a light-scheme
         * theme, flip the `dark` class before first paint so they don't get a
         * black frame while React hydrates. Reads the same key the theme store
         * persists; a malformed payload is ignored and the dark default stays. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=JSON.parse(localStorage.getItem('be_theme_v1')||'null');if(t&&t.scheme==='light'){document.documentElement.classList.remove('dark');}}catch(e){}`,
          }}
        />
        {/* Capture ?setup=1 BEFORE the app shell rewrites the URL to ?tab=chat.
         * The gate reads this session flag, so the setup link can never be
         * lost during hydration. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{if(new URLSearchParams(location.search).get('setup')==='1'){sessionStorage.setItem('be_setup_force','1');}}catch(e){}`,
          }}
        />
        <ThemeHydrator />
        {/* Everything that polls, streams or checks for updates is held back
         * while a first-run wizard owns the screen; it has nothing to show and
         * would compete with the wizard's save for the browser's connections. */}
        <AppActivity pending={setupPending}>
          <GlobalLoader />
          <RiskAckGate />
          <AgentationMount />
          <GlobalSearchMount />
          <KeyboardShortcuts />
          <TerminalKeybind />
          <WorkspaceKeybind />
          <SoundProvider />
          <TtsProvider />
          <AlertNotificationProvider />
          <ConfirmDialogHost />
          <TaskCompletionWatcher />
          <BotsRunWatcher />
          <ChannelActivityWatcher />
          <NewsAlertWatcher />
          <BrowserTabIndicator />
          <DesktopTaskbarBadge />
          <UpdateBanner />
          <DesktopSession />
          <DesktopShellGuards />
        </AppActivity>
        {/* Always mounted: a connection loss is not a poller and must show
         * during onboarding and every tab, not only when AppActivity is live. */}
        <OfflineBanner />
        <OnboardingGate setupPending={setupPending} />
        {children}
      </body>
    </html>
  );
}
