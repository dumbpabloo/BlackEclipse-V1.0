'use client';

import React, { useState, useEffect, useMemo, useRef } from 'react';
import { Newspaper, Sparkles, Search, X, SlidersHorizontal, TrendingUp, TrendingDown, RefreshCw, Bookmark, Star, AlertTriangle, Bell } from '@/lib/icons';
import { Button } from '@/components/ui/button';
import { TabBar } from '@/components/ui/tab-bar';
import { TopLoader } from '@/components/ui/top-loader';
import { Skeleton } from '@/components/ui/skeleton';
import { usePageShortcuts } from '@/hooks/use-page-shortcuts';
import { useListNavigation } from '@/hooks/use-list-navigation';
import { loadPortfolioSymbols } from '@/lib/news/client-context';
import { NewsPagination } from '@/components/screens/news-pagination';
import { NewsSavedView } from '@/components/screens/news-saved-view';
import { NewsDetailDialog } from '@/components/screens/news-detail-dialog';
import { PageSizeInput } from '@/components/ui/pagination';
import { DataAttribution } from '@/components/screens/data-attribution';
import { adoptLegacySaves, loadSaved, saveSaved, saveLegacyIds } from '@/lib/news/saved';
import { relativeTime } from '@/lib/news/relative-time';
import { openExternal } from '@/lib/desktop/external';
import {
  clearCardSettings,
  DEFAULT_CARD_SETTINGS,
  loadCardSettings,
  saveCardSettings,
  onCardSettingsChange,
  clampPerPage,
  MAX_PER_PAGE,
  type CardSettings,
} from '@/lib/news/card-settings';

export interface NewsItem {
  id: string;
  source: string;
  category?: string;
  region?: string;
  tier?: number;
  ticker?: string;
  timestamp: string;
  headline: string;
  summary: string;
  sentiment: 'bullish' | 'bearish' | 'neutral';
  url?: string;
  sourceCount?: number;      // story cluster: how many sources covered it
  clusterSources?: string[]; // story cluster: source names
  // Deterministic importance fields returned by /api/news. Optional so cached
  // payloads and the compact widget keep working unchanged.
  importance?: number;
  alertLevel?: 'critical' | 'high' | 'medium' | 'low';
  importanceReasons?: string[];
  isBreaking?: boolean;
  confidence?: 'high' | 'medium' | 'low';
  direction?: 'bullish' | 'bearish' | 'neutral';
  horizon?: 'intraday' | 'days' | 'macro';
  invalidates?: string[];
  affected?: string[];
}

export interface NewsPulse {
  bullish?: number;
  bearish?: number;
  neutral?: number;
}

// Relative time comes from `lib/news/relative-time` (shared with saved view).

interface MarketNewsProps {
  onDiscussNews?: (news: NewsItem) => void;
  onAnalyzeTicker?: (ticker: string) => void;
  fullPage?: boolean;
  /** Receives the tier-weighted 24h news sentiment score in [-1, 1]. */
  onNewsSentiment?: (score: number) => void;
  /**
   * Narrow the feed to a single ticker (set by global search's "SYMBOL news"
   * related pivot). Changing it re-seeds the `ticker:` query operator.
   */
  symbol?: string;
  /** False while this screen sits in the shell's hidden keep-alive slot. */
  active?: boolean;
}

const CATEGORIES = [
  'MARKETS', 'STOCKS', 'BONDS', 'FOREX', 'CRYPTO', 'COMMODITIES',
  'ECONOMIC', 'INFLATION', 'CENTRAL BANKS', 'JOBS', 'TRADE',
  'GEOPOLITICS', 'WAR', 'POLITICS', 'DEFENSE',
  'REGULATORY', 'TECH', 'AI', 'ENERGY', 'HEALTHCARE', 'AUTO',
  'CLIMATE',
] as const;
const REGIONS = ['US', 'GLOBAL', 'EU', 'UK', 'ASIA', 'INDIA', 'MENA'] as const;
const TIERS = [
  { value: '1', label: 'T1 Wires' },
  { value: '2', label: 'T2 Media' },
  { value: '3', label: 'T3 Analysis' },
  { value: '4', label: 'T4 Blogs' },
] as const;
const SENTIMENTS = ['bullish', 'bearish', 'neutral'] as const;

interface FilterState {
  category: string;
  region: string;
  tier: string;
  sentiment: string;
  q: string;
}

const EMPTY_FILTERS: FilterState = { category: '', region: '', tier: '', sentiment: '', q: '' };

// ─── Card customization constants ──────────────────────────────────────

const CARD_COLUMNS_CLASS: Record<CardSettings['columns'], string> = {
  1: 'grid-cols-1',
  2: 'grid-cols-1 lg:grid-cols-2',
  3: 'grid-cols-1 lg:grid-cols-2 xl:grid-cols-3',
};

const DENSITY_CLASS: Record<CardSettings['density'], { card: string; title: string; body: string }> = {
  compact: { card: 'p-3.5 gap-3', title: 'text-sm', body: 'text-[11px]' },
  cozy: { card: 'p-5 gap-4', title: 'text-base', body: 'text-xs' },
};

// Static map — Tailwind's JIT can't see dynamically interpolated classes.
const LINE_CLAMP_CLASS: Record<CardSettings['summaryLines'], string> = {
  0: '',
  2: 'line-clamp-2',
  3: 'line-clamp-3',
  4: 'line-clamp-4',
};



function FilterSelect({ value, onChange, placeholder, options, selectRef }: {
  selectRef?: React.Ref<HTMLSelectElement>;
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  options: readonly { value: string; label: string }[] | readonly string[];
}) {
  return (
    <div className="relative h-9">
      <select
        ref={selectRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`h-9 appearance-none rounded-lg bg-accent/30 border pl-3 pr-8 text-[12px] font-mono cursor-pointer focus:outline-none focus:border-primary/60 transition-colors ${
          value ? 'text-primary border-primary/40 bg-primary/10' : 'text-muted-foreground border-border/30'
        }`}
      >
        <option value="">{placeholder}</option>
        {options.map((o) => {
          const val = typeof o === 'string' ? o : o.value;
          const label = typeof o === 'string' ? o : o.label;
          return <option key={val} value={val} className="bg-background text-foreground">{label}</option>;
        })}
      </select>
      <span className={`pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[9px] ${value ? 'text-primary' : 'text-muted-foreground/60'}`}>▼</span>
    </div>
  );
}

// ─── Price Reaction mini chart removed — no longer shown.

export function MarketNews({ onDiscussNews, fullPage = false, onNewsSentiment, symbol, active = true }: MarketNewsProps) {
  const [selectedNews, setSelectedNews] = useState<NewsItem | null>(null);
  const [newsList, setNewsList] = useState<NewsItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [totalItems, setTotalItems] = useState(0);
  const [filters, setFilters] = useState<FilterState>(EMPTY_FILTERS);
  const [pendingQ, setPendingQ] = useState('');
  const [cards, setCards] = useState<CardSettings>(DEFAULT_CARD_SETTINGS);
  const [showCustomize, setShowCustomize] = useState(false);
  const [pulse, setPulse] = useState<NewsPulse>({});
  const [lastIngestAt, setLastIngestAt] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [newCount, setNewCount] = useState(0);
  const [stagedNews, setStagedNews] = useState<NewsItem[]>([]);
  const [myStocksOnly, setMyStocksOnly] = useState(false);
  const [watchTickers, setWatchTickers] = useState<string[]>([]);
  const [savedView, setSavedView] = useState(false);
  const [savedItems, setSavedItems] = useState<NewsItem[]>([]);
  const [legacySavedIds, setLegacySavedIds] = useState<Set<string>>(new Set());
  const savedIds = useMemo(
    () => new Set([...savedItems.map((item) => item.id), ...legacySavedIds]),
    [savedItems, legacySavedIds],
  );
  const [highAlertOnly, setHighAlertOnly] = useState(false);
  const [portfolioTickers, setPortfolioTickers] = useState<string[]>([]);
  const [alertNotice, setAlertNotice] = useState<string | null>(null);
  const [actingAlertId, setActingAlertId] = useState<string | null>(null);

  // Guards against out-of-order responses when the user clicks categories /
  // filters quickly. Each fetchNews() call bumps this counter; only the
  // response matching the latest counter is applied, so a slow older request
  // can never overwrite a newer one (or leave the UI stuck on the loader).
  const fetchSeqRef = useRef(0);

  useEffect(() => {
    setCards(loadCardSettings());
    // Watchlist tickers (shared with the watchlist screen's storage).
    try {
      const raw = window.localStorage.getItem('watchlist_guest');
      if (raw) {
        const parsed = JSON.parse(raw);
        const syms = (Array.isArray(parsed) ? parsed : []).map((w: { symbol?: string }) => String(w.symbol || '').toUpperCase()).filter(Boolean);
        setWatchTickers(syms.slice(0, 100));
      }
    } catch { /* ignore */ }
    const saved = loadSaved();
    setSavedItems(saved.items);
    setLegacySavedIds(new Set(saved.legacyIds));
    // Portfolio holdings are best-effort (Python engine may be unavailable).
    // When present they raise relevance for stories touching a real position.
    void loadPortfolioSymbols().then((syms) => { if (syms.length > 0) setPortfolioTickers(syms); });

    // Card settings may be edited from Settings › News while this tab stays
    // mounted behind the modal; re-read them live.
    return onCardSettingsChange(() => setCards(loadCardSettings()));
  }, []);

  const updateCards = (patch: Partial<CardSettings>) => {
    setCards((prev) => {
      const next = { ...prev, ...patch };
      saveCardSettings(next);
      return next;
    });
  };

  const resetCards = () => {
    clearCardSettings();
    setCards(DEFAULT_CARD_SETTINGS);
  };

  const activeFilterCount = useMemo(
    () => Object.values(filters).filter(Boolean).length,
    [filters]
  );

  const fetchNews = async (
    pageNum = 1,
    f: FilterState = filters,
    c: CardSettings = cards,
    opts?: { myStocks?: boolean; highAlert?: boolean },
  ) => {
    const seq = ++fetchSeqRef.current;
    setLoading(true);
    setError(null);
    try {
      const useMyStocks = opts?.myStocks ?? myStocksOnly;
      const useHighAlert = opts?.highAlert ?? highAlertOnly;
      const effectiveSort = useHighAlert ? 'importance' : c.sort;
      const perPage = clampPerPage(c.perPage);
      const params = new URLSearchParams({ page: String(pageNum), limit: String(perPage), sort: effectiveSort });
      if (useHighAlert) params.set('minImportance', '55');
      if (f.category) params.set('category', f.category);
      if (f.region) params.set('region', f.region);
      if (f.tier) params.set('tier', f.tier);
      if (f.sentiment) params.set('sentiment', f.sentiment);
      if (f.q) params.set('q', f.q);
      if (useMyStocks && watchTickers.length > 0) params.set('tickers', watchTickers.join(','));
      if (portfolioTickers.length > 0) params.set('portfolio', portfolioTickers.join(','));
      const res = await fetch(`/api/news?${params.toString()}`);
      if (!res.ok) {
        // Surface the server-provided actionable message (e.g. corruption hint)
        // instead of a generic status code.
        let apiError = `API ${res.status}`;
        try {
          const errBody = await res.json();
          if (errBody?.error) apiError = errBody.error;
        } catch { /* non-JSON body — keep default */ }
        throw new Error(apiError);
      }
      const data = await res.json();
      // Drop stale responses: a newer fetch has already started.
      if (seq !== fetchSeqRef.current) return;
      const news: NewsItem[] = data.news || [];
      setNewsList(news);
      // Move any ID-only legacy bookmark whose article just arrived into the
      // snapshot store so the Saved view can actually render it.
      if (legacySavedIds.size > 0) {
        const adopted = adoptLegacySaves(news, savedItems, legacySavedIds);
        if (adopted.items !== savedItems) {
          setSavedItems(adopted.items);
          setLegacySavedIds(adopted.legacyIds);
        }
      }
      setPage(pageNum);
      setTotalPages(data.pagination?.totalPages || 1);
      setTotalItems(data.pagination?.total || news.length);
      if (data.pulse) setPulse(data.pulse);
      if (data.lastIngestAt) setLastIngestAt(data.lastIngestAt);
      // Overall news sentiment: tier-weighted score from the API when present,
      // falling back to raw page-item counts for cached payloads.
      if (typeof data.newsScore === 'number') {
        onNewsSentiment?.(data.newsScore);
      } else {
        const bullish = news.filter((n: NewsItem) => n.sentiment === 'bullish').length;
        const bearish = news.filter((n: NewsItem) => n.sentiment === 'bearish').length;
        const denom = bullish + bearish;
        onNewsSentiment?.(denom > 0 ? (bullish - bearish) / denom : 0);
      }
    } catch (e) {
      // Only surface errors from the latest request.
      if (seq !== fetchSeqRef.current) return;
      console.error("[MarketNews] Error fetching news:", e);
      setError('Could not load the news database. It may still be ingesting — try again in a minute.');
      setNewsList([]);
      setTotalItems(0);
      setPage(pageNum);
      setTotalPages(1);
    } finally {
      // Only the latest request may clear the loading state.
      if (seq === fetchSeqRef.current) setLoading(false);
    }
  };

  // Manual refresh: trigger a background ingestion cycle, then re-fetch.
  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await fetch('/api/cron/refresh-news', { method: 'POST' });
      setStagedNews([]);
      setNewCount(0);
      await fetchNews(1);
    } catch {
      // refresh trigger failure is non-fatal; list refresh alone is fine
      await fetchNews(1);
    } finally {
      setRefreshing(false);
    }
  };

  // One-click price watch from any card with a resolved ticker. Reuses the
  // existing /api/news/alert endpoint (previously orphaned).
  const createPriceAlert = async (item: NewsItem) => {
    if (!item.ticker) return;
    setActingAlertId(item.id);
    setAlertNotice(null);
    try {
      const direction = item.sentiment === 'bearish' ? 'down' : 'up';
      const res = await fetch('/api/news/alert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker: item.ticker, direction, movePct: 2 }),
      });
      const data = await res.json().catch(() => ({}));
      setAlertNotice(
        res.ok
          ? `Price watch created for ${item.ticker} (${direction} ≥ 2%).`
          : data?.error || 'Could not create the price watch.',
      );
    } catch {
      setAlertNotice('Could not create the price watch.');
    } finally {
      setActingAlertId(null);
    }
  };

  useEffect(() => {
    fetchNews(1);
    // fetchNews is stable per-render; only run on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Symbol pin from global search ("AAPL news"). The news API parses a
  // `ticker:` operator out of the free-text `q`, so we set that instead of
  // adding a new filter dimension. An empty symbol clears the pin.
  const pinnedSymbol = (symbol || '').toUpperCase();
  useEffect(() => {
    const next: FilterState = { ...EMPTY_FILTERS, q: pinnedSymbol ? `ticker:${pinnedSymbol}` : '' };
    setPendingQ(next.q);
    setMyStocksOnly(false);
    setSavedView(false);
    setHighAlertOnly(false);
    setFilters(next);
    fetchNews(1, next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pinnedSymbol]);

  // ─── Auto-refresh: silent background polling ─────────────────────────
  // Fetches page 1 quietly (no loading skeleton), compares against the
  // displayed list, and stages new items behind a "N new articles" pill
  // instead of yanking the list out from under the user. Under importance
  // ranking new stories already belong at the top, so the ranked page is
  // replaced in place instead. Only polls while the tab is visible; refetches
  // immediately when the tab regains focus.
  const pollForNew = async () => {
    // The High Alert filter is a user-pinned query; leave it alone.
    if (highAlertOnly) return;
    try {
      const params = new URLSearchParams({ page: '1', limit: String(clampPerPage(cards.perPage)), sort: cards.sort });
      if (filters.category) params.set('category', filters.category);
      if (filters.region) params.set('region', filters.region);
      if (filters.tier) params.set('tier', filters.tier);
      if (filters.sentiment) params.set('sentiment', filters.sentiment);
      if (filters.q) params.set('q', filters.q);
      const res = await fetch(`/api/news?${params.toString()}`);
      if (!res.ok) return;
      const data = await res.json();
      const incoming: NewsItem[] = data.news || [];
      if (incoming.length === 0) return;
      // New = items with an id not currently displayed or staged.
      const known = new Set([...newsList, ...stagedNews].map((n) => n.id));
      const fresh = incoming.filter((n) => !known.has(n.id));
      if (fresh.length > 0 && page === 1) {
        if (cards.sort === 'importance') {
          // Ranking order shifts as new stories arrive — swap the page quietly.
          setNewsList(incoming);
          setTotalPages(data.pagination?.totalPages || 1);
          setTotalItems(data.pagination?.total || incoming.length);
        } else {
          setStagedNews((prev) => {
            const seen = new Set(prev.map((n) => n.id));
            return [...prev, ...fresh.filter((n) => !seen.has(n.id))];
          });
          setNewCount((prev) => prev + fresh.filter((n) => !known.has(n.id)).length);
        }
        if (data.pulse) setPulse(data.pulse);
        if (data.lastIngestAt) setLastIngestAt(data.lastIngestAt);
      }
    } catch {
      // silent — polling failures must never surface as UI errors
    }
  };

  useEffect(() => {
    if (!active || !cards.autoRefresh) return;
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') void pollForNew();
    }, cards.refreshSec * 1000);
    return () => clearInterval(id);
    // pollForNew closes over current state; re-arm when deps change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, cards.autoRefresh, cards.refreshSec, cards.perPage, cards.sort, filters, newsList, stagedNews, page]);

  // Instant refresh when the user returns to the tab after being away.
  useEffect(() => {
    const onVisible = () => {
      if (active && document.visibilityState === 'visible') void pollForNew();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  });

  const showStaged = () => {
    setNewsList((prev) => {
      const seen = new Set(prev.map((n) => n.id));
      return [...stagedNews.filter((n) => !seen.has(n.id)), ...prev].slice(0, clampPerPage(cards.perPage));
    });
    setStagedNews([]);
    setNewCount(0);
  };

  const toggleSaved = (item: NewsItem) => {
    setSavedItems((prev) => {
      const exists = prev.some((s) => s.id === item.id);
      const next = exists ? prev.filter((s) => s.id !== item.id) : [item, ...prev];
      saveSaved(next);
      return next;
    });
    setLegacySavedIds((prev) => {
      if (!prev.has(item.id)) return prev;
      const next = new Set(prev);
      next.delete(item.id);
      saveLegacyIds(next);
      return next;
    });
  };

  const openSavedView = () => {
    setSavedView(true);
    setMyStocksOnly(false);
    setHighAlertOnly(false);
  };

  const closeSavedView = () => {
    setSavedView(false);
    void fetchNews(1);
  };

  const openNews = (item: NewsItem) => {
    // No popup — open the source article in the real browser (the desktop
    // shell owns external links; a new-window request is not hosted there).
    if (item.url) {
      if (!openExternal(item.url)) window.open(item.url, '_blank', 'noopener,noreferrer');
      return;
    }
    // Fallback: no URL on this item — show the in-app briefing modal.
    setSelectedNews(item);
  };

  const handleDiscuss = (item?: NewsItem) => {
    const target = item || selectedNews;
    if (target) {
      onDiscussNews?.(target);
      setSelectedNews(null);
    }
  };

  const handlePageChange = (newPage: number) => {
    if (newPage >= 1 && newPage <= totalPages) {
      fetchNews(newPage);
    }
  };

  const applyFilters = (next: FilterState) => {
    setFilters(next);
    fetchNews(1, next);
  };

  const setFilter = (patch: Partial<FilterState>) => {
    // Use the latest filters (functional update) so rapid category clicks
    // never read a stale closure and fire the wrong query.
    setFilters((prev) => {
      const next = { ...prev, ...patch };
      // Toggling filters off entirely resets to the default view.
      if (!next.category && !next.region && !next.tier && !next.sentiment && !next.q) {
        fetchNews(1, EMPTY_FILTERS);
        return EMPTY_FILTERS;
      }
      fetchNews(1, next);
      return next;
    });
  };

  const clearFilters = () => {
    setPendingQ('');
    setMyStocksOnly(false);
    setSavedView(false);
    setHighAlertOnly(false);
    applyFilters(EMPTY_FILTERS);
  };

  // News Details / AI Briefing Modal — shared by widget + full-page views.
  const newsDialog = selectedNews ? (
    <NewsDetailDialog
      item={selectedNews}
      saved={savedIds.has(selectedNews.id)}
      onClose={() => setSelectedNews(null)}
      onToggleSaved={() => toggleSaved(selectedNews)}
      onDiscuss={() => handleDiscuss()}
    />
  ) : null;

  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const filterRef = useRef<HTMLSelectElement>(null);
  const newsIds = useMemo(() => newsList.map((n) => n.id), [newsList]);
  const listNav = useListNavigation({
    ids: fullPage ? newsIds : [],
    rootRef,
    enabled: fullPage && !loading && !selectedNews,
    shortcutPrefix: 'news.list',
    onOpen: (id) => {
      const item = newsList.find((n) => n.id === id);
      if (item) openNews(item);
    },
    pagination: {
      onPrevious: () => handlePageChange(page - 1),
      onNext: () => handlePageChange(page + 1),
      canPrevious: page > 1,
      canNext: page < totalPages,
    },
  });
  usePageShortcuts(rootRef, [
    { id: 'news.search', label: 'Search news', keys: 'S', run: () => searchRef.current?.focus(), enabled: fullPage && !loading },
    { id: 'news.filters', label: 'Focus news filters', keys: 'Shift+F', run: () => filterRef.current?.focus(), enabled: fullPage && !loading },
    { id: 'news.customize', label: 'Customize news cards', keys: 'Shift+C', run: () => setShowCustomize((v) => !v), enabled: fullPage && !loading },
    { id: 'news.refresh', label: 'Refresh news', keys: 'R', run: handleRefresh, enabled: fullPage && !loading && !refreshing },
  ]);

  // Saved is its own searchable/filterable/paginated list backed by persisted
  // snapshots — not a filter over whichever feed page happens to be loaded.
  if (fullPage && savedView) {
    return (
      <div ref={rootRef} data-keyboard-scope="Saved news" className="w-full flex flex-col gap-6 select-none">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h2 className="text-lg font-serif font-semibold text-foreground flex items-center gap-2">
              <Bookmark size={16} className="text-amber-400" weight="fill" /> Saved articles
            </h2>
            <p className="text-xs text-muted-foreground">Bookmarked snapshots — search, filter and page independently of the live feed.</p>
          </div>
          <button onClick={closeSavedView}
            className="h-9 px-3 rounded-lg bg-accent/30 border border-border/30 text-[12px] font-semibold text-muted-foreground hover:text-foreground cursor-pointer">
            Back to feed
          </button>
        </div>
        <NewsSavedView
          items={savedItems}
          onOpen={openNews}
          onRemove={toggleSaved}
          onDiscuss={(item) => handleDiscuss(item)}
          onBack={closeSavedView}
        />
        {newsDialog}
      </div>
    );
  }

  if (loading) {
    const loadingBar = <TopLoader />;

    if (fullPage) {
      return (
        <>
          {loadingBar}
          <div className="w-full flex flex-col gap-4">
            {[1, 2, 3, 4, 5].map(i => (
              <div key={i} className="flex flex-col md:flex-row md:items-center justify-between gap-4 border border-border/10 rounded-2xl p-5">
                <div className="flex-1 space-y-3">
                  <div className="flex gap-2">
                    <Skeleton className="h-4 w-16" />
                    <Skeleton className="h-4 w-12" />
                    <Skeleton className="h-4 w-20" />
                  </div>
                  <Skeleton className="h-6 w-3/4" />
                  <div className="space-y-1.5">
                    <Skeleton className="h-3.5 w-full" />
                    <Skeleton className="h-3.5 w-5/6" />
                  </div>
                </div>
                <div className="shrink-0 min-w-[200px] border-t md:border-t-0 md:border-l border-border/10 pt-4 md:pt-0 md:pl-6 space-y-3">
                  <Skeleton className="h-5 w-24 rounded-full ml-auto" />
                  <div className="flex gap-2">
                    <Skeleton className="h-8.5 flex-1 rounded-xl" />
                    <Skeleton className="h-8.5 flex-1 rounded-xl" />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      );
    }
    return (
      <>
        {loadingBar}
        <div className="w-full flex flex-col rounded-2xl p-4 gap-4 border border-border/10">
          <Skeleton className="h-4 w-28" />
          <div className="space-y-3">
            {[1, 2, 3].map(i => (
              <div key={i} className="p-3 border border-border/10 rounded-xl space-y-2">
                <div className="flex justify-between">
                  <Skeleton className="h-3 w-12" />
                  <Skeleton className="h-3 w-10" />
                </div>
                <Skeleton className="h-4 w-5/6" />
                <Skeleton className="h-3 w-16 rounded-full" />
              </div>
            ))}
          </div>
        </div>
      </>
    );
  }

  if (error && newsList.length === 0) {
    return (
      <div className="w-full text-center py-10 font-mono text-xs text-muted-foreground border border-dashed border-border/25 rounded-2xl p-6 glass-card-subtle">
        <p className="text-rose-400 font-bold mb-2">Error: {error}</p>
        <button onClick={() => fetchNews()} className="px-4 py-2 bg-primary text-primary-foreground font-semibold rounded-xl cursor-pointer hover:bg-primary/90 transition-all">
          Retry News Feed Load
        </button>
      </div>
    );
  }

  if (fullPage) {
    return (
      <div ref={rootRef} data-keyboard-scope="Market news" className="w-full flex flex-col gap-6 select-none">
        <DataAttribution extra={["Official regulator / central-bank RSS feeds"]} />
        {/* High-level stories are not a separate band: they are ranked into the
            main feed (most-critical first) and surfaced in the header ledger by
            `NewsAlertWatcher`. */}

        {/* ─── Filter Bar ─── */}
        <div className="w-full rounded-2xl border border-border/10 glass-card p-4 flex flex-col lg:flex-row lg:items-center gap-3">
          <form
            className="flex items-center gap-2 flex-1 min-w-0"
            onSubmit={(e) => { e.preventDefault(); setFilter({ q: pendingQ.trim() }); }}
          >
            <div className="relative flex-1 min-w-0 max-w-md">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                ref={searchRef}
                value={pendingQ}
                onChange={(e) => setPendingQ(e.target.value)}
                placeholder="Search… try ticker:NVDA  sentiment:bearish  category:WAR"
                className="h-9 w-full rounded-lg bg-accent/30 border border-border/30 pl-9 pr-8 text-[12px] font-mono text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:border-primary/60 transition-colors"
              />
              {pendingQ && (
                <button
                  type="button"
                  onClick={() => { setPendingQ(''); if (filters.q) setFilter({ q: '' }); }}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer"
                >
                  <X size={13} />
                </button>
              )}
            </div>
            <button
              type="submit"
              className="h-9 px-4 shrink-0 rounded-lg bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 text-[12px] font-semibold cursor-pointer transition-colors"
            >
              Search
            </button>
          </form>

          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => {
                const next = !highAlertOnly;
                setHighAlertOnly(next);
                setMyStocksOnly(false);
                setSavedView(false);
                fetchNews(1, filters, cards, { highAlert: next, myStocks: false });
              }}
              title="Only market-moving stories (importance score ≥ 55), strongest first"
              className={`h-9 px-3 rounded-lg text-[12px] font-bold inline-flex items-center gap-1.5 cursor-pointer transition-colors border ${
                highAlertOnly
                  ? 'bg-amber-400/15 text-amber-300 border-amber-400/40'
                  : 'bg-accent/30 text-muted-foreground border-border/30 hover:text-foreground'
              }`}
            >
              <AlertTriangle size={12} weight={highAlertOnly ? 'fill' : 'regular'} /> High alert
            </button>
            <button
              onClick={() => {
                const next = !myStocksOnly;
                setMyStocksOnly(next);
                setSavedView(false);
                setHighAlertOnly(false);
                fetchNews(1, filters, cards, { myStocks: next, highAlert: false });
              }}
              disabled={watchTickers.length === 0}
              title={watchTickers.length === 0 ? 'Add tickers to your watchlist first' : `Articles matching your ${watchTickers.length} watchlist tickers`}
              className={`h-9 px-3 rounded-lg text-[12px] font-bold inline-flex items-center gap-1.5 cursor-pointer transition-colors border disabled:opacity-40 disabled:cursor-not-allowed ${
                myStocksOnly
                  ? 'bg-primary/15 text-primary border-primary/40'
                  : 'bg-accent/30 text-muted-foreground border-border/30 hover:text-foreground'
              }`}
            >
              <Star size={12} weight={myStocksOnly ? 'fill' : 'regular'} /> My Stocks
            </button>
            <button
              onClick={() => {
                if (savedView) closeSavedView(); else openSavedView();
              }}
              title={savedIds.size === 0 ? 'Bookmark articles with the ribbon icon' : `${savedIds.size} bookmarked articles`}
              className={`h-9 px-3 rounded-lg text-[12px] font-bold inline-flex items-center gap-1.5 cursor-pointer transition-colors border ${
                savedView
                  ? 'bg-amber-400/15 text-amber-400 border-amber-400/40'
                  : 'bg-accent/30 text-muted-foreground border-border/30 hover:text-foreground'
              }`}
            >
              <Bookmark size={12} weight={savedView ? 'fill' : 'regular'} /> Saved{savedIds.size > 0 ? ` (${savedIds.size})` : ''}
            </button>
            <FilterSelect
              selectRef={filterRef}
              value={filters.category}
              placeholder="Category"
              options={CATEGORIES}
              onChange={(v) => setFilter({ category: v })}
            />
            <FilterSelect
              value={filters.region}
              placeholder="Region"
              options={REGIONS}
              onChange={(v) => setFilter({ region: v })}
            />
            <FilterSelect
              value={filters.tier}
              placeholder="Source Tier"
              options={TIERS}
              onChange={(v) => setFilter({ tier: v })}
            />
            <FilterSelect
              value={filters.sentiment}
              placeholder="Sentiment"
              options={SENTIMENTS}
              onChange={(v) => setFilter({ sentiment: v })}
            />
            {activeFilterCount > 0 && (
              <button
                onClick={clearFilters}
                className="h-9 px-3 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/25 text-[11px] font-mono font-semibold cursor-pointer transition-colors inline-flex items-center gap-1.5"
              >
                <X size={12} /> Clear ({activeFilterCount})
              </button>
            )}
          </div>
        </div>

        {/* ─── Market Pulse — 24h sentiment split across all ingested news ─── */}
        {(() => {
          const b = pulse.bullish ?? 0;
          const r = pulse.bearish ?? 0;
          const n = pulse.neutral ?? 0;
          const totalPulse = b + r + n;
          if (totalPulse === 0) return null;
          const pct = (v: number) => (totalPulse > 0 ? Math.round((v / totalPulse) * 100) : 0);
          const skew = b >= r ? 'bullish' : 'bearish';
          return (
            <div className="w-full rounded-2xl border border-border/10 glass-card p-4 flex flex-col md:flex-row md:items-center gap-4">
              <div className="flex items-center gap-2 shrink-0">
                {skew === 'bullish'
                  ? <TrendingUp size={16} className="text-emerald-400" />
                  : <TrendingDown size={16} className="text-rose-400" />}
                <div>
                  <p className="text-[10px] uppercase font-bold tracking-wider text-muted-foreground">Market Pulse · 24h</p>
                  <p className="text-xs font-bold text-foreground">
                    {skew === 'bullish' ? 'Bullish tilt' : 'Bearish tilt'}
                    <span className="text-muted-foreground font-mono font-normal"> · {totalPulse} articles</span>
                  </p>
                </div>
              </div>
              <div className="flex-1 min-w-0">
                <div className="h-3 w-full rounded-full overflow-hidden flex bg-accent/30 border border-border/20">
                  <div className="h-full bg-emerald-500/70" style={{ width: `${pct(b)}%` }} title={`${b} bullish`} />
                  <div className="h-full bg-neutral-500/60" style={{ width: `${pct(n)}%` }} title={`${n} neutral`} />
                  <div className="h-full bg-rose-500/70" style={{ width: `${pct(r)}%` }} title={`${r} bearish`} />
                </div>
                <div className="flex items-center justify-between mt-1.5 font-mono text-[10px] text-muted-foreground">
                  <span className="text-emerald-400">{pct(b)}% bullish</span>
                  <span>{pct(n)}% neutral</span>
                  <span className="text-rose-400">{pct(r)}% bearish</span>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {lastIngestAt && (
                  <span className="text-[10px] font-mono text-muted-foreground hidden lg:inline" title="Last feed ingestion">
                    Updated {relativeTime(lastIngestAt)}
                  </span>
                )}
                <button
                  onClick={handleRefresh}
                  disabled={refreshing}
                  className="h-8 px-3 rounded-lg bg-accent/40 hover:bg-accent/70 border border-border/40 text-muted-foreground hover:text-primary text-[11px] font-bold inline-flex items-center gap-1.5 cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-wait"
                  title="Re-ingest all feeds now"
                >
                  <RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} /> Refresh
                </button>
              </div>
            </div>
          );
        })()}

        {/* ─── Category Tabs — one-tap browse, below Market Pulse ─── */}
        <div className="w-full rounded-2xl border border-border/10 glass-card px-4 py-3">
          <TabBar
            className="mb-0"
            items={[
              { id: '', label: 'All', active: !filters.category },
              ...CATEGORIES.map((cat) => ({
                id: cat,
                label: cat === 'CENTRAL BANKS' ? 'Central Banks' : cat === 'GEOPOLITICS' ? 'Geopolitics' : cat === 'REGULATORY' ? 'Regulatory' : cat === 'ECONOMIC' ? 'Economic' : cat.charAt(0) + cat.slice(1).toLowerCase(),
                active: filters.category === cat,
              })),
            ]}
            onChange={(id) => setFilter({ category: id === '' ? '' : (filters.category === id ? '' : id) })}
          />
        </div>

        {/* ─── Result count + Customize ─── */}
        <div className="flex items-center justify-between gap-3 font-mono text-[11px] text-muted-foreground px-1 flex-wrap">
          <span className="flex items-center gap-2 min-w-0">
            <Newspaper size={12} className="text-primary" />
            <span><span className="text-foreground font-bold">{totalItems}</span> articles</span>
            <span className="text-[10px] text-muted-foreground font-mono hidden md:inline">
              {activeFilterCount > 0 ? `filtered · page ${page} of ${totalPages}` : `page ${page} of ${totalPages}`}
            </span>
          </span>
          <button
            onClick={() => setShowCustomize((v) => !v)}
            className={`flex items-center gap-1.5 text-xs font-bold tracking-wider border rounded-lg px-3.5 py-2 transition-all cursor-pointer shrink-0 ${
              showCustomize
                ? 'text-primary bg-primary/10 border-primary/40'
                : 'text-muted-foreground hover:text-primary bg-accent/40 hover:bg-accent/70 border-border/40'
            }`}
            title="Customize how cards look"
          >
            <SlidersHorizontal size={14} /> Customize
          </button>
        </div>

        {/* Inline config panel — expands in flow, pushes content down */}
        {showCustomize && (
          <div className="p-3 sm:p-4 rounded-xl glass-card border border-white/10 dark:border-white/5 shadow-lg w-full max-w-full overflow-hidden animate-fade-in-up space-y-4">
            <div className="flex items-center justify-between">
              <p className="text-[10px] font-bold text-foreground uppercase tracking-wider">Card Preferences</p>
              <button
                onClick={resetCards}
                className="text-[10px] font-bold text-muted-foreground hover:text-rose-400 uppercase tracking-wider cursor-pointer transition-colors"
              >
                Reset to defaults
              </button>
            </div>

            {/* Feed / page size */}
            <div>
              <p className="text-[9px] uppercase font-bold text-muted-foreground/70 tracking-wider mb-1.5">Feed</p>
              <div className="flex flex-wrap gap-1.5">
                <span className="text-[10px] text-muted-foreground font-bold uppercase tracking-wider self-center">Cards per page:</span>
                {([10, 20, 30, 50] as const).map((n) => (
                  <button
                    key={n}
                    onClick={() => {
                      updateCards({ perPage: n });
                      fetchNews(1, filters, { ...cards, perPage: n });
                    }}
                    className={`px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border whitespace-nowrap ${
                      cards.perPage === n ? 'bg-primary/15 text-primary border-primary/40' : 'text-muted-foreground border-border/40 hover:bg-accent/40'
                    }`}
                  >
                    {n}
                  </button>
                ))}
                <PageSizeInput
                  pageSize={cards.perPage}
                  max={MAX_PER_PAGE}
                  label="Custom"
                  className="self-center"
                  onCommit={(n) => {
                    updateCards({ perPage: n });
                    fetchNews(1, filters, { ...cards, perPage: n });
                  }}
                />
                <span className="mx-2 w-px self-stretch bg-border/60" aria-hidden />
                <span className="text-[10px] text-muted-foreground font-bold uppercase tracking-wider self-center">Sort:</span>
                {(['newest', 'oldest', 'importance'] as const).map((s) => (
                  <button
                    key={s}
                    onClick={() => {
                      updateCards({ sort: s });
                      fetchNews(1, filters, { ...cards, sort: s });
                    }}
                    className={`px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border capitalize whitespace-nowrap ${
                      cards.sort === s ? 'bg-primary/15 text-primary border-primary/40' : 'text-muted-foreground border-border/40 hover:bg-accent/40'
                    }`}
                  >
                    {s === 'importance' ? 'Critical first' : s}
                  </button>
                ))}
                <span className="mx-2 w-px self-stretch bg-border/60" aria-hidden />
                <span className="text-[10px] text-muted-foreground font-bold uppercase tracking-wider self-center">Auto-refresh:</span>
                <button
                  onClick={() => updateCards({ autoRefresh: !cards.autoRefresh })}
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border whitespace-nowrap ${
                    cards.autoRefresh ? 'bg-primary/10 border-primary/40 text-primary' : 'bg-transparent border-border/40 text-muted-foreground opacity-60 hover:opacity-100'
                  }`}
                >
                  <span className={`w-1.5 h-1.5 rounded-full ${cards.autoRefresh ? 'bg-primary' : 'bg-border'}`} />
                  {cards.autoRefresh ? 'On' : 'Off'}
                </button>
                {([30, 60, 120] as const).map((s) => (
                  <button
                    key={s}
                    onClick={() => updateCards({ refreshSec: s })}
                    className={`px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border whitespace-nowrap ${
                      cards.refreshSec === s ? 'bg-primary/15 text-primary border-primary/40' : 'text-muted-foreground border-border/40 hover:bg-accent/40'
                    }`}
                  >
                    {s}s
                  </button>
                ))}
              </div>
            </div>

            {/* Layout */}
            <div>
              <p className="text-[9px] uppercase font-bold text-muted-foreground/70 tracking-wider mb-1.5">Layout</p>
              <div className="flex flex-wrap gap-1.5">
                <span className="text-[10px] text-muted-foreground font-bold uppercase tracking-wider self-center">Columns:</span>
                {[1, 2, 3].map((n) => (
                  <button
                    key={n}
                    onClick={() => updateCards({ columns: n as CardSettings['columns'] })}
                    className={`px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border whitespace-nowrap ${
                      cards.columns === n ? 'bg-primary/15 text-primary border-primary/40' : 'text-muted-foreground border-border/40 hover:bg-accent/40'
                    }`}
                  >
                    {n === 1 ? 'Single' : `${n} cols`}
                  </button>
                ))}
                <span className="mx-2 w-px self-stretch bg-border/60" aria-hidden />
                <span className="text-[10px] text-muted-foreground font-bold uppercase tracking-wider self-center">Density:</span>
                {(['compact', 'cozy'] as const).map((d) => (
                  <button
                    key={d}
                    onClick={() => updateCards({ density: d })}
                    className={`px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border capitalize whitespace-nowrap ${
                      cards.density === d ? 'bg-primary/15 text-primary border-primary/40' : 'text-muted-foreground border-border/40 hover:bg-accent/40'
                    }`}
                  >
                    {d}
                  </button>
                ))}
                <span className="mx-2 w-px self-stretch bg-border/60" aria-hidden />
                <span className="text-[10px] text-muted-foreground font-bold uppercase tracking-wider self-center">Summary:</span>
                {([2, 3, 4, 0] as const).map((n) => (
                  <button
                    key={n}
                    onClick={() => updateCards({ summaryLines: n })}
                    className={`px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border whitespace-nowrap ${
                      cards.summaryLines === n ? 'bg-primary/15 text-primary border-primary/40' : 'text-muted-foreground border-border/40 hover:bg-accent/40'
                    }`}
                  >
                    {n === 0 ? 'Full' : `${n} lines`}
                  </button>
                ))}
              </div>
            </div>

            {/* Visibility toggles */}
            <div>
              <p className="text-[9px] uppercase font-bold text-muted-foreground/70 tracking-wider mb-1.5">Show on cards</p>
              <div className="flex flex-wrap gap-1.5">
                {([
                  ['showSummary', 'Summary'],
                  ['showBadges', 'Badges'],
                  ['showTimestamp', 'Timestamp'],
                  ['showSentiment', 'Sentiment'],
                  ['showButtons', 'Buttons'],
                  ['glow', 'Glow'],
                ] as [keyof CardSettings, string][]).map(([key, label]) => {
                  const on = Boolean(cards[key]);
                  return (
                    <button
                      key={key}
                      onClick={() => updateCards({ [key]: !on } as Partial<CardSettings>)}
                      className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] font-bold transition-all cursor-pointer border whitespace-nowrap ${
                        on ? 'bg-primary/10 border-primary/40 text-primary' : 'bg-transparent border-border/40 text-muted-foreground opacity-60 hover:opacity-100'
                      }`}
                    >
                      <span className={`w-1.5 h-1.5 rounded-full ${on ? 'bg-primary' : 'bg-border'}`} />
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        )}

        {/* ─── New-articles pill (staged by auto-refresh polling) ─── */}
        {newCount > 0 && (
          <button
            onClick={showStaged}
            className="w-full py-2.5 rounded-xl bg-primary/10 hover:bg-primary/20 border border-primary/30 text-primary text-[12px] font-bold font-mono inline-flex items-center justify-center gap-2 cursor-pointer transition-colors animate-fade-in-up"
          >
            <RefreshCw size={13} /> {newCount} new article{newCount > 1 ? 's' : ''} — click to show
          </button>
        )}

        {alertNotice && (
          <div className="flex items-center gap-1.5 px-1 font-mono text-[10px] text-muted-foreground">
            <Bell size={11} className="text-primary" /> {alertNotice}
          </div>
        )}

        <div className={`${CARD_COLUMNS_CLASS[cards.columns]} grid gap-4.5 w-full`}>
          {newsList.length === 0 && (
            <div className="w-full text-center py-12 font-mono text-xs text-muted-foreground border border-dashed border-border/25 rounded-2xl glass-card-subtle">
              {savedView ? (
                <>
                  <p className="mb-3">No bookmarked articles yet — tap the ribbon icon on any card.</p>
                  <button onClick={clearFilters} className="px-4 py-2 bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 rounded-xl text-[11px] font-semibold cursor-pointer transition-colors">
                    Back to all news
                  </button>
                </>
              ) : myStocksOnly ? (
                <>
                  <p className="mb-3">No news for your watchlist tickers right now. Add tickers on the Watchlist page or check back later.</p>
                  <button onClick={clearFilters} className="px-4 py-2 bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 rounded-xl text-[11px] font-semibold cursor-pointer transition-colors">
                    Show all news
                  </button>
                </>
              ) : (
                <>
                  <p className="mb-3">No articles match the current filters.</p>
                  <button onClick={clearFilters} className="px-4 py-2 bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 rounded-xl text-[11px] font-semibold cursor-pointer transition-colors">
                    Clear Filters
                  </button>
                </>
              )}
            </div>
          )}
          {newsList.map(item => {
            const isBullish = item.sentiment === 'bullish';
            const isBearish = item.sentiment === 'bearish';
            const dens = DENSITY_CLASS[cards.density];

            // Dynamic card colors & glows based on sentiment score
            const borderStyle = isBullish
              ? 'border-emerald-500/25 dark:border-emerald-500/15 hover:border-emerald-500/60'
              : isBearish
              ? 'border-rose-500/25 dark:border-rose-500/15 hover:border-rose-500/60'
              : 'border-white/10 dark:border-white/5 hover:border-primary/50';

            const bgGradient = isBullish
              ? 'bg-gradient-to-br from-emerald-500/[0.03] via-[#0f0f0f]/60 to-[#0f0f0f]/30 backdrop-blur-xl'
              : isBearish
              ? 'bg-gradient-to-br from-rose-500/[0.03] via-[#0f0f0f]/60 to-[#0f0f0f]/30 backdrop-blur-xl'
              : 'glass-card';

            const summaryClass =
              cards.showSummary && cards.summaryLines > 0
                ? LINE_CLAMP_CLASS[cards.summaryLines]
                : '';

            return (
              <div
                role="button"
                tabIndex={0}
                aria-label={item.headline}
                onKeyDown={(e) => { if (e.key === 'Enter' && listNav.selectedId === null && (e.target as HTMLElement).getAttribute('role') === 'button') { e.preventDefault(); openNews(item); } }}
                key={item.id}
                {...listNav.rowAttributes(item.id)}
                onClick={() => openNews(item)}
                className={`rounded-2xl border hover:shadow-lg transition-all duration-300 flex flex-col justify-between relative overflow-hidden text-left shadow-sm group cursor-pointer hover:scale-[1.005] focus:outline-none ${listNav.isSelected(item.id) ? 'ring-2 ring-primary/60 ring-offset-1 ring-offset-background' : ''} ${dens.card} ${borderStyle} ${bgGradient}`}
              >
                {/* Ambient glow badge based on sentiment */}
                {cards.glow && (
                  <div
                    className={`absolute top-0 right-0 -translate-y-1/2 translate-x-1/2 w-48 h-48 rounded-full blur-[60px] pointer-events-none opacity-20 transition-all duration-300 ${
                      isBullish ? 'bg-emerald-500' : isBearish ? 'bg-rose-500' : 'bg-primary'
                    }`}
                  />
                )}

                <div className="flex-1 min-w-0 space-y-2.5 relative z-10">
                  {(cards.showBadges || cards.showTimestamp) && (
                    <div className="flex items-center gap-2 flex-wrap">
                      {cards.showBadges && item.source && (
                        <span className="text-[10px] font-bold text-muted-foreground font-mono uppercase bg-accent/60 px-2 py-0.5 rounded border border-border/40">
                          {item.source}
                        </span>
                      )}
                      {cards.showBadges && item.category && (
                        <span className="text-[10px] font-bold text-primary/80 font-mono uppercase bg-primary/5 border border-primary/15 px-2 py-0.5 rounded">
                          {item.category}
                        </span>
                      )}
                      {cards.showBadges && item.ticker && (
                        <span className="text-[10px] font-extrabold text-primary font-mono bg-primary/10 border border-primary/20 px-2 py-0.5 rounded">
                          {item.ticker}
                        </span>
                      )}
                      {cards.showBadges && (item.sourceCount ?? 1) > 1 && (
                        <span
                          className="text-[10px] font-bold text-amber-400 font-mono bg-amber-400/5 border border-amber-400/25 px-2 py-0.5 rounded"
                          title={`Covered by: ${(item.clusterSources || []).join(', ')}`}
                        >
                          ×{item.sourceCount} sources
                        </span>
                      )}
                      {cards.showBadges && item.alertLevel === 'critical' && (
                        <span className="text-[10px] font-bold text-rose-300 font-mono bg-rose-500/10 border border-rose-500/35 px-2 py-0.5 rounded">
                          CRITICAL
                        </span>
                      )}
                      {cards.showBadges && item.alertLevel === 'high' && (
                        <span className="text-[10px] font-bold text-amber-300 font-mono bg-amber-400/10 border border-amber-400/30 px-2 py-0.5 rounded">
                          HIGH ALERT
                        </span>
                      )}
                      {cards.showBadges && item.isBreaking && (
                        <span className="text-[10px] font-bold text-rose-300 font-mono bg-rose-500/10 border border-rose-500/30 px-2 py-0.5 rounded">
                          BREAKING
                        </span>
                      )}
                      {/* Timestamp is its own toggle — it used to be nested inside
                          the Badges block, so turning Badges off silently hid it. */}
                      {cards.showTimestamp && (
                        <span className="text-[10px] text-muted-foreground font-mono">{relativeTime(item.timestamp)}</span>
                      )}
                    </div>
                  )}

                  <h3 className={`font-semibold leading-snug text-foreground group-hover:text-primary transition-colors ${dens.title}`}>
                    {item.headline}
                  </h3>

                  {cards.showSummary && (
                    <p className={`${dens.body} text-muted-foreground leading-relaxed ${summaryClass}`}>
                      {item.summary || 'Open the article for the full story.'}
                    </p>
                  )}

                  {cards.showSentiment && (
                    <div className="flex items-center gap-3 pt-0.5">
                      <span
                        className={`text-[9px] font-bold uppercase tracking-wider px-2.5 py-0.5 rounded-full ${
                          isBullish
                            ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/35 shadow-[0_0_8px_rgba(16,185,129,0.08)]'
                            : isBearish
                            ? 'bg-rose-500/10 text-rose-400 border border-rose-500/35 shadow-[0_0_8px_rgba(244,63,94,0.08)]'
                            : 'bg-neutral-500/10 text-neutral-400 border border-neutral-500/35'
                        }`}
                      >
                        {item.sentiment}
                      </span>
                      {item.region && item.region !== 'GLOBAL' && (
                        <span className="text-[10px] text-muted-foreground/80 font-mono">{item.region}</span>
                      )}
                    </div>
                  )}
                </div>

                {/* Footer action row — full width, below the content */}
                {cards.showButtons && (
                  <div className="flex items-center gap-2 border-t border-border/40 pt-3 relative z-10">
                    <button
                      onClick={(e) => { e.stopPropagation(); toggleSaved(item); }}
                      className={`h-8 w-8 rounded-lg border inline-flex items-center justify-center cursor-pointer transition-colors ${
                        savedIds.has(item.id)
                          ? 'bg-amber-400/10 border-amber-400/40 text-amber-400'
                          : 'border-border hover:bg-accent text-muted-foreground'
                      }`}
                      title={savedIds.has(item.id) ? 'Remove bookmark' : 'Bookmark article'}
                    >
                      <Bookmark size={13} weight={savedIds.has(item.id) ? 'fill' : 'regular'} />
                    </button>
                    {item.ticker && (
                      <button
                        onClick={(e) => { e.stopPropagation(); void createPriceAlert(item); }}
                        disabled={actingAlertId === item.id}
                        className="h-8 w-8 rounded-lg border border-border hover:bg-accent text-muted-foreground inline-flex items-center justify-center cursor-pointer transition-colors disabled:opacity-40"
                        title={`Create a price watch for ${item.ticker}`}
                      >
                        <Bell size={13} />
                      </button>
                    )}
                    <span className="flex-1" />
                    <Button
                      size="sm"
                      onClick={(e) => { e.stopPropagation(); handleDiscuss(item); }}
                      className="text-[11px] h-8 rounded-lg px-4 inline-flex items-center justify-center gap-1.5 bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 hover:border-primary/40 font-semibold cursor-pointer transition-all"
                    >
                      <Sparkles size={11} /> Discuss with AI
                     </Button>
                   </div>
                 )}
               </div>
             );
           })}
        </div>

        {/* Pagination Controls — page numbers, typed go-to-page, typed page size */}
        {totalItems > 0 && (
          <NewsPagination
            page={page}
            totalPages={totalPages}
            totalItems={totalItems}
            perPage={clampPerPage(cards.perPage)}
            maxPerPage={MAX_PER_PAGE}
            onPage={handlePageChange}
            onPerPage={(n) => {
              updateCards({ perPage: n });
              fetchNews(1, filters, { ...cards, perPage: n });
            }}
          />
        )}
        {newsDialog}
      </div>
    );
  }

  return (
    <div className="w-full flex flex-col glass-card border border-white/10 dark:border-white/5 rounded-2xl p-4 overflow-hidden relative">
      <div className="flex items-center gap-2 mb-4 shrink-0">
        <Newspaper className="text-primary w-4.5 h-4.5" />
        <span className="font-serif text-sm font-semibold tracking-tight">AI Financial Briefings</span>
      </div>

      {/* News list */}
      <div className="space-y-3.5 overflow-y-auto max-h-[300px] scrollbar-hide pr-0.5">
        {newsList.map(item => {
          const isBullish = item.sentiment === 'bullish';
          const isBearish = item.sentiment === 'bearish';

          const cardBorder = isBullish
            ? 'border-emerald-500/15 hover:border-emerald-500/40 bg-emerald-950/[0.01]'
            : isBearish
            ? 'border-rose-500/15 hover:border-rose-500/40 bg-rose-950/[0.01]'
            : 'border-border/10 glass-card-subtle hover:border-border/30';

          return (
            <button
              type="button"
              key={item.id}
              onClick={() => openNews(item)}
              className={`p-3 rounded-xl border hover:bg-accent/30 hover:shadow-md transition-all duration-300 flex flex-col gap-1.5 cursor-pointer text-left group focus:outline-none ${cardBorder}`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-bold text-muted-foreground font-mono uppercase bg-accent/40 px-1.5 py-0.5 rounded">
                    {item.source}
                  </span>
                  {item.category && (
                    <span className="text-[10px] font-bold text-primary/80 font-mono uppercase bg-primary/5 border border-primary/15 px-1.5 py-0.5 rounded">
                      {item.category}
                    </span>
                  )}
                  {item.ticker && (
                    <span className="text-[10px] font-extrabold text-primary font-mono bg-primary/10 border border-primary/20 px-1.5 py-0.5 rounded">
                      {item.ticker}
                    </span>
                  )}
                  {item.alertLevel === 'critical' && (
                    <span className="text-[9px] font-bold text-rose-300 font-mono bg-rose-500/10 border border-rose-500/30 px-1.5 py-0.5 rounded">
                      CRITICAL
                    </span>
                  )}
                  {item.alertLevel === 'high' && (
                    <span className="text-[9px] font-bold text-amber-300 font-mono bg-amber-400/10 border border-amber-400/30 px-1.5 py-0.5 rounded">
                      HIGH
                    </span>
                  )}
                </div>
                <span className="text-[9px] text-muted-foreground font-mono">{relativeTime(item.timestamp)}</span>
              </div>

              <h3 className="text-xs font-semibold leading-snug text-foreground group-hover:text-primary transition-colors">
                {item.headline}
              </h3>

              {/* Sentiment Badge */}
              <div className="flex items-center justify-between mt-0.5">
                <span
                  className={`text-[9px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full ${
                    isBullish
                      ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                      : isBearish
                      ? 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
                      : 'bg-neutral-500/10 text-neutral-400 border border-neutral-500/20'
                  }`}
                >
                  {item.sentiment}
                </span>
                <span className="text-[9px] text-muted-foreground/80 font-mono">
                  {relativeTime(item.timestamp)}
                </span>
              </div>
            </button>
          );
        })}
      </div>

      {/* News Details / AI Briefing Modal — shared by widget + full-page views */}
      {newsDialog}
    </div>
  );
}
