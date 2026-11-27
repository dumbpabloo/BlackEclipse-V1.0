// Mutation guard + rate limiter for API routes.
//
// BlackEclipse is an open-source, zero-config app: cloning the repo and
// launching the desktop app must work with no environment setup. So protection
// is layered by deployment mode instead of requiring accounts:
//
//   1. Browser UI (same-origin POSTs)          -> always allowed.
//   2. Server-to-server with CRON_SECRET set   -> allowed via
//      `Authorization: Bearer <CRON_SECRET>` or `x-api-key`.
//   3. Non-browser requests without a secret    -> only allowed when the
//      server itself is local (localhost / 127.0.0.1). On a public host,
//      setting CRON_SECRET is required for programmatic access.
//
// This keeps the out-of-the-box dev experience frictionless while making a
// public deployment safe with a single env var.

// `0.0.0.0` is deliberately NOT trusted here: it is a wildcard bind address,
// not a loopback host, and treating a request whose Host is `0.0.0.0` as local
// would let a non-loopback deployment skip the secret requirement. Only true
// loopback hosts are local.
//
// `blackeclipse` is the repo's former local hostname alias (a hosts-file entry
// mapping it to 127.0.0.1, fronted by a reverse proxy that has since been
// deleted). It is kept in the set because the alias still resolves to loopback
// on machines that configured it, so it is the same local origin as `localhost` —
// without this, requests arriving through the hostname would be judged remote
// (blocking no-Origin programmatic calls and forcing a Secure cookie over plain
// http).
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1", "blackeclipse"]);

/**
 * The hostname of a host:port pair, lowercased, brackets removed. Handles IPv6
 * (`[::1]:3000` -> `::1`).
 */
function hostnameOf(host: string): string {
  const raw = (host || "").trim().toLowerCase();
  if (!raw) return "";
  if (raw.startsWith("[")) {
    const close = raw.indexOf("]");
    return close >= 0 ? raw.slice(1, close) : raw.replace(/[[\]]/g, "");
  }
  return raw.split(":")[0];
}

/**
 * Normalize a host for SAME-ORIGIN comparison. Every loopback alias
 * (`localhost`, `127.0.0.1`, `::1`) names the SAME machine, so they are folded to
 * one key (keeping the port). A browser on `localhost:3000` posting to a server
 * whose `request.url` says `127.0.0.1:3000` is the same origin and must be
 * allowed; treating them as different is what produced spurious 403s and made
 * the whole UI's mutations fail. A real (non-loopback) host is returned
 * unchanged, so cross-site requests are still blocked.
 */
function normalizeHost(host: string): string {
  const name = hostnameOf(host);
  if (LOCAL_HOSTS.has(name)) {
    const port = (host || "").includes(":") ? (host || "").split(":").pop() : "";
    return `loopback:${port ?? ""}`;
  }
  return (host || "").toLowerCase();
}

function requestHost(request: Request): string {
  // Prefer the host the request was actually addressed to. `request.url` is a
  // poor source behind a proxy: Next.js rebuilds it from its own bind address
  // in dev (e.g. `http://127.0.0.1:3000/...`) even when the browser used a
  // different hostname, which would make a same-origin POST look cross-origin.
  //
  // `x-forwarded-host` is caller-controlled unless a trusted reverse proxy sets
  // it, and trusting it unconditionally let a non-browser caller claim to be
  // loopback. It is honoured only when the operator explicitly declares a
  // trusted proxy with `BE_TRUST_PROXY=1`; the Host header is the default.
  const trustProxy = /^(1|true|yes|on)$/i.test((process.env.BE_TRUST_PROXY ?? "").trim());
  if (trustProxy) {
    const forwarded = request.headers.get("x-forwarded-host");
    if (forwarded) {
      const first = forwarded.split(",")[0].trim();
      if (first) return first;
    }
  }
  const host = request.headers.get("host");
  if (host) return host;
  try {
    return new URL(request.url).host;
  } catch {
    return "";
  }
}

function originHost(request: Request): string | null {
  const origin = request.headers.get("origin");
  if (!origin || origin === "null") return null;
  try {
    return new URL(origin).host;
  } catch {
    return null;
  }
}

export function isLocalHost(host: string): boolean {
  return LOCAL_HOSTS.has(hostnameOf(host));
}

/** True when the request was addressed to a loopback/local host (Host-aware). */
export function isLocalRequest(request: Request): boolean {
  return isLocalHost(requestHost(request));
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export interface GuardResult {
  allowed: boolean;
  reason?: string;
}

/**
 * Decide whether a mutating (POST/PUT/PATCH/DELETE) request may proceed.
 * Returns { allowed: true } or { allowed: false, reason }.
 */
export function checkMutationAllowed(request: Request): GuardResult {
  const host = requestHost(request);
  const origin = originHost(request);

  // 1) Explicit secret wins (cron jobs, scripts, reverse-proxied calls).
  const secret = process.env.CRON_SECRET;
  if (secret && secret !== "change-me-to-a-random-secret") {
    const auth = request.headers.get("authorization") || "";
    const bearer = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    const apiKey = request.headers.get("x-api-key") || "";
    if (timingSafeEqual(bearer, secret) || timingSafeEqual(apiKey, secret)) {
      return { allowed: true };
    }
  }

  // 2) Same-origin browser requests are trusted (the app's own UI).
  //    Browsers always attach Origin to cross-site POSTs; a matching Origin
  //    means the call came from a page served by this app. Loopback aliases
  //    (`localhost` / `127.0.0.1` / `::1`) are the same origin, so they are
  //    compared normalized rather than byte-for-byte.
  if (origin !== null) {
    if (normalizeHost(origin) === normalizeHost(host)) return { allowed: true };
    // Origin present but genuinely mismatched -> cross-site request forgery.
    return { allowed: false, reason: "Cross-origin requests are not allowed" };
  }

  // 3) No Origin header (curl, cron, another service). Allowed only when the
  //    server is local, or when the request targets a local loopback host.
  if (isLocalHost(host)) return { allowed: true };

  return {
    allowed: false,
    reason: "Set CRON_SECRET and send it as 'Authorization: Bearer <secret>' for programmatic access",
  };
}

/**
 * Convenience wrapper for route handlers: returns a 403 NextResponse when the
 * request is rejected, otherwise null (continue).
 */
export function guardMutation(
  request: Request,
  label: string
): { status: number; body: { error: string } } | null {
  const result = checkMutationAllowed(request);
  if (result.allowed) return null;
  console.warn(`[guard] blocked ${label}: ${result.reason}`);
  return { status: 403, body: { error: result.reason || "Forbidden" } };
}

// ─── Rate limiting ──────────────────────────────────────────────────────
// Simple in-memory fixed-window limiter. Adequate for the single-node,
// self-hosted deployments this app targets; swap for a shared store when
// running multiple instances.

interface Bucket {
  count: number;
  windowStart: number;
  windowMs: number;
}

const buckets = new Map<string, Bucket>();

// Periodically drop stale buckets so the map cannot grow unbounded.
const SWEEP_INTERVAL_MS = 60_000;
let lastSweep = Date.now();

function sweep(now: number) {
  if (now - lastSweep < SWEEP_INTERVAL_MS) return;
  lastSweep = now;
  for (const [key, bucket] of buckets) {
    if (now - bucket.windowStart > bucket.windowMs) buckets.delete(key);
  }
}

export interface RateLimitOptions {
  /** Max requests allowed per window. */
  limit: number;
  /** Window length in milliseconds. */
  windowMs: number;
}

export interface RateLimitResult {
  ok: boolean;
  remaining: number;
  retryAfterSec: number;
}

export function rateLimit(key: string, options: RateLimitOptions): RateLimitResult {
  const now = Date.now();
  sweep(now);
  const bucket = buckets.get(key);
  if (!bucket || now - bucket.windowStart >= options.windowMs) {
    buckets.set(key, { count: 1, windowStart: now, windowMs: options.windowMs });
    return { ok: true, remaining: options.limit - 1, retryAfterSec: 0 };
  }
  bucket.count++;
  if (bucket.count > options.limit) {
    return {
      ok: false,
      remaining: 0,
      retryAfterSec: Math.max(1, Math.ceil((bucket.windowStart + options.windowMs - now) / 1000)),
    };
  }
  return { ok: true, remaining: options.limit - bucket.count, retryAfterSec: 0 };
}

/** Best-effort client identity for rate limiting (proxy-aware). */
export function clientKey(request: Request): string {
  const forwarded = request.headers.get("x-forwarded-for");
  if (forwarded) return forwarded.split(",")[0].trim();
  const realIp = request.headers.get("x-real-ip");
  if (realIp) return realIp.trim();
  // Desktop tabs share one loopback host, so a host-only key made the limiter
  // GLOBAL across every chat tab: one busy tab could 429 the other. The client
  // sends a per-tab id; it is combined with the host so the bucket stays scoped
  // to this app (a caller rotating the header only affects its own local bucket,
  // and this app is single-operator/loopback by default).
  const tab = request.headers.get("x-be-client");
  if (tab && /^[A-Za-z0-9_-]{8,64}$/.test(tab)) {
    return `${requestHost(request)}:${tab}`;
  }
  return requestHost(request) || "unknown";
}

// ─── Outbound fetch safety (SSRF) ───────────────────────────────────────
// User-configurable URLs (news feeds) are fetched server-side. Restrict them
// to public http(s) endpoints so a malicious feed config cannot probe
// localhost services or internal network hosts.

const PRIVATE_HOST_PATTERNS = [
  /^localhost$/i,
  /^127\./,
  /^0\./,
  /^10\./,
  /^169\.254\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
  /^192\.168\./,
  /^\[?::1\]?$/,
  /^\[?fe80:/i,
  /^\[?fc00:/i,
  /^\[?fd[0-9a-f]{2}:/i,
  /\.local$/i,
  /\.internal$/i,
];

export interface UrlSafetyResult {
  safe: boolean;
  reason?: string;
}

export function checkOutboundUrl(rawUrl: string): UrlSafetyResult {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return { safe: false, reason: "Invalid URL" };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { safe: false, reason: `Blocked protocol: ${parsed.protocol}` };
  }
  if (parsed.username || parsed.password) {
    return { safe: false, reason: "URLs with embedded credentials are not allowed" };
  }
  const hostname = parsed.hostname;
  if (PRIVATE_HOST_PATTERNS.some((re) => re.test(hostname))) {
    return { safe: false, reason: `Blocked host: ${hostname}` };
  }
  // Pure-IPv4/IPv6 literals are risky even when they don't match the common
  // private ranges above — require a real hostname.
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(hostname)) {
    return { safe: false, reason: "IP-literal URLs are not allowed" };
  }
  return { safe: true };
}

/** Filter a list of feed URLs down to the ones safe to fetch server-side. */
export function filterSafeUrls<T extends { url: string }>(items: T[]): T[] {
  return items.filter((item) => checkOutboundUrl(item.url).safe);
}

/**
 * Combined guard for expensive mutation endpoints: mutation policy + per-IP
 * rate limit in one call. Returns a NextResponse-ready rejection or null.
 */
export function guardExpensiveMutation(
  request: Request,
  label: string,
  limits: RateLimitOptions
): { status: number; body: { error: string }; headers?: Record<string, string> } | null {
  const forbidden = guardMutation(request, label);
  if (forbidden) return forbidden;
  const rl = rateLimit(`${label}:${clientKey(request)}`, limits);
  if (!rl.ok) {
    return {
      status: 429,
      body: { error: "Too many requests — slow down" },
      headers: { "Retry-After": String(rl.retryAfterSec) },
    };
  }
  return null;
}

// ─── Identity / tenancy deployment guard ────────────────────────────────
// BlackEclipse is a single-user local app: there is NO authentication and NO
// tenancy isolation. `userId`/`scopeId` are caller-supplied scope labels, not
// identities. This guard never claims multi-user safety; it only reports the
// current exposure so a public deployment (or a missing secret) is visible in
// the logs rather than silently mistaken for secure.

export interface DeploymentExposure {
  /** True when this process does not appear to be loopback-only. */
  exposed: boolean;
  /** Whether a real (non-placeholder) CRON_SECRET is configured. */
  operatorSecretConfigured: boolean;
  /** Always false — multi-user safety is never claimed. */
  multiUserSafe: false;
  /** Human-readable warning to log, when exposed. */
  warning?: string;
}

export function deploymentExposure(env: Record<string, string | undefined> = process.env): DeploymentExposure {
  const secret = env.CRON_SECRET;
  const operatorSecretConfigured = Boolean(secret) && secret !== "change-me-to-a-random-secret";
  // A server that is not explicitly bound loopback and has no secret to require
  // programmatic callers to present is exposed.
  const bindHost = (env.HOST ?? "").trim().toLowerCase();
  const nonLoopbackBind = Boolean(bindHost) && !["localhost", "127.0.0.1", "::1", "[::1]"].includes(bindHost);
  const exposed = !operatorSecretConfigured && (nonLoopbackBind || env.NODE_ENV === "production");
  const warning = exposed
    ? "No CRON_SECRET configured. This app is single-user local and has NO authentication or tenancy isolation (userId/scopeId are self-asserted labels). Do not expose it publicly; set CRON_SECRET and/or run it behind an authenticated, single-operator boundary."
    : undefined;
  return { exposed, operatorSecretConfigured, multiUserSafe: false, warning };
}

/** Log the exposure assessment once at boot (never throws). */
export function warnOnDeploymentExposure(env: Record<string, string | undefined> = process.env): DeploymentExposure {
  const exposure = deploymentExposure(env);
  if (exposure.exposed && exposure.warning) {
    console.warn(`[guard] DEPLOYMENT WARNING: ${exposure.warning}`);
  }
  return exposure;
}
