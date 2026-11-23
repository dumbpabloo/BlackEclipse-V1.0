"use client";

// The broker connection form. Extracted from the dialog so the shell stays
// readable and under the source size budget.
//
// The broker-type list is the registry's own (`/trading/brokers`), not a
// hardcoded four: any declared kind can be connected here, and the credential /
// endpoint fields follow that kind's auth style.

import { Handshake, Loader2 } from "@/lib/icons";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { BROKER_RESPONSIBILITY_NOTICE } from "@/lib/legal/disclosure";
import type { BrokerKind } from "@/lib/harness/clients/trading-client";

export type BrokerKindId = string;

export const inputClass =
  "w-full rounded-xl border border-border/40 bg-transparent px-2.5 py-1.5 text-xs text-foreground focus:border-primary/40 focus:outline-none";

export function ConnectionForm({
  kind,
  setKind,
  kinds,
  allowKindChange,
  label,
  setLabel,
  mode,
  setMode,
  terminalPath,
  setTerminalPath,
  bridgeUrl,
  setBridgeUrl,
  bridgeSecretRef,
  setBridgeSecretRef,
  baseUrl,
  setBaseUrl,
  dataUrl,
  setDataUrl,
  credentialRef,
  setCredentialRef,
  brokerAccountId,
  setBrokerAccountId,
  quoteAsset,
  setQuoteAsset,
  magic,
  setMagic,
  maxMargin,
  setMaxMargin,
  busy,
  onSubmit,
  submitLabel,
  compact = false,
}: {
  kind: BrokerKindId;
  setKind: (value: BrokerKindId) => void;
  kinds: BrokerKind[];
  allowKindChange: boolean;
  label: string;
  setLabel: (value: string) => void;
  mode: "demo" | "live";
  setMode: (value: "demo" | "live") => void;
  terminalPath: string;
  setTerminalPath: (value: string) => void;
  bridgeUrl: string;
  setBridgeUrl: (value: string) => void;
  bridgeSecretRef: string;
  setBridgeSecretRef: (value: string) => void;
  baseUrl: string;
  setBaseUrl: (value: string) => void;
  dataUrl: string;
  setDataUrl: (value: string) => void;
  credentialRef: string;
  setCredentialRef: (value: string) => void;
  brokerAccountId: string;
  setBrokerAccountId: (value: string) => void;
  quoteAsset: string;
  setQuoteAsset: (value: string) => void;
  magic: string;
  setMagic: (value: string) => void;
  maxMargin: string;
  setMaxMargin: (value: string) => void;
  busy: boolean;
  onSubmit: () => void;
  submitLabel: string;
  compact?: boolean;
}) {
  const isBridge = kind === "mt4_bridge";
  const isMt5 = kind === "mt5";
  const isRest = !isBridge && !isMt5;
  const isAlpaca = kind === "alpaca";
  const isIbkr = kind === "ibkr";
  const selected = kinds.find((entry) => entry.id === kind);
  const endpointPlaceholder = isAlpaca
    ? "https://paper-api.alpaca.markets"
    : isIbkr
      ? "https://localhost:5000/v1/api"
      : "blank = the broker's default endpoint";
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <label className="col-span-2 space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Broker type</span>
          {allowKindChange ? (
            <Select
              value={kind}
              onValueChange={(value) => {
                if (!value) return;
                setKind(value);
                const next = kinds.find((entry) => entry.id === value);
                if (next && !next.modes.includes("demo") && mode === "demo") {
                  setMode("live");
                }
                // A blank label follows the selected kind instead of silently
                // saving the previous broker's name.
                if (next && !label.trim()) setLabel(next.label);
              }}
            >
              <SelectTrigger className={cn(inputClass, "h-auto w-full justify-between py-1.5")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="min-w-[var(--anchor-width)] max-h-72">
                {kinds.length === 0 && (
                  <SelectItem value="__unavailable" disabled>
                    No broker registry (runtime offline)
                  </SelectItem>
                )}
                {kinds.map((entry) => (
                  <SelectItem key={entry.id} value={entry.id} disabled={!entry.available}>
                    {entry.label}
                    {entry.available ? "" : " (unavailable)"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <p className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground">
              {selected?.label ?? kind}
            </p>
          )}
          {allowKindChange && selected && !selected.available && (
            <span className="block text-[9px] text-amber-200/80">
              {selected.unavailableReason ?? "This broker is unavailable on this host."}
            </span>
          )}
        </label>
        <label className="col-span-2 space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Label</span>
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder={selected?.label ?? "Connection label"}
            className={inputClass}
          />
        </label>
        <label className="space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Mode</span>
          <Select
            value={mode}
            onValueChange={(value) => setMode(value === "live" ? "live" : "demo")}
          >
            <SelectTrigger className={cn(inputClass, "h-auto w-full justify-between py-1.5")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="min-w-[var(--anchor-width)]">
              <SelectItem value="demo" disabled={Boolean(selected && !selected.modes.includes("demo"))}>
                Demo
              </SelectItem>
              <SelectItem value="live">Live (arm in Settings)</SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label className="space-y-1">
          <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
            Max margin / order (live)
          </span>
          <input
            value={maxMargin}
            onChange={(event) => setMaxMargin(event.target.value)}
            placeholder="required to arm live"
            className={inputClass}
          />
        </label>
        {(isMt5 || isBridge) && (
          <label className="space-y-1">
            <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Magic</span>
            <input value={magic} onChange={(event) => setMagic(event.target.value)} className={inputClass} />
          </label>
        )}
        {isBridge && (
          <>
            <label className="col-span-2 space-y-1">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                Bridge URL
              </span>
              <input
                value={bridgeUrl}
                onChange={(event) => setBridgeUrl(event.target.value)}
                placeholder="http://127.0.0.1:8090"
                className={inputClass}
              />
            </label>
            <label className="col-span-2 space-y-1">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                Secret name (env var or secret:// ref, optional)
              </span>
              <input
                value={bridgeSecretRef}
                onChange={(event) => setBridgeSecretRef(event.target.value)}
                placeholder="MT4_BRIDGE_TOKEN"
                className={inputClass}
              />
            </label>
          </>
        )}
        {isMt5 && (
          <label className="col-span-2 space-y-1">
            <span className="text-[9px] uppercase tracking-widest text-muted-foreground">Terminal path</span>
            <input
              value={terminalPath}
              onChange={(event) => setTerminalPath(event.target.value)}
              className={inputClass}
            />
          </label>
        )}
        {isRest && (
          <>
            <label className="col-span-2 space-y-1">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                {isIbkr ? "Client Portal gateway URL" : "API base URL (optional)"}
              </span>
              <input
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder={endpointPlaceholder}
                className={inputClass}
              />
            </label>
            {isAlpaca && (
              <label className="col-span-2 space-y-1">
                <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                  Market-data base URL
                </span>
                <input
                  value={dataUrl}
                  onChange={(event) => setDataUrl(event.target.value)}
                  placeholder="https://data.alpaca.markets"
                  className={inputClass}
                />
              </label>
            )}
            <label className="col-span-2 space-y-1">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                Credential name (env var or secret:// ref)
              </span>
              <input
                value={credentialRef}
                onChange={(event) => setCredentialRef(event.target.value)}
                placeholder={
                  isAlpaca
                    ? "ALPACA_KEYS"
                    : isIbkr
                      ? "IBKR_GATEWAY_COOKIE"
                      : "BROKER_KEYS"
                }
                className={inputClass}
              />
            </label>
            <label className="col-span-2 space-y-1">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                Broker account id (optional)
              </span>
              <input
                value={brokerAccountId}
                onChange={(event) => setBrokerAccountId(event.target.value)}
                placeholder={isIbkr ? "DU1234567" : "discovered when blank"}
                className={inputClass}
              />
            </label>
            <label className="col-span-2 space-y-1">
              <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                Quote asset for spot closes (optional)
              </span>
              <input
                value={quoteAsset}
                onChange={(event) => setQuoteAsset(event.target.value)}
                placeholder="USDT / USD — blank uses the venue's listed pairs"
                className={inputClass}
              />
            </label>
          </>
        )}
      </div>
      {!compact && (
        <p className="text-[9.5px] text-muted-foreground">
          {isBridge ? (
            <>
              The bridge runs next to MT4 and owns the login. Eclipse reads the documented JSON
              contract and, when the bridge implements the write endpoints, can place, cancel, close
              and modify. Point it at loopback.
            </>
          ) : isMt5 ? (
            <>
              No password is stored: Eclipse attaches to the account already logged into the terminal.
              Enable <span className="font-medium text-foreground">Algo Trading</span> in MT5 before
              placing any order.
            </>
          ) : (
            <>
              Every REST kind places orders through the same preview → approval → single-send path.
              The credential is a NAME (env var or secret:// ref), never a value; a paper/sandbox host
              is the demo account. Journal P&amp;L for these brokers is FIFO-computed from executions.
            </>
          )}
        </p>
      )}
      {!compact && (
        <p className="text-[9.5px] leading-relaxed text-muted-foreground/70">
          {BROKER_RESPONSIBILITY_NOTICE}
        </p>
      )}
      <Button type="button" size="sm" className="w-full" onClick={onSubmit} disabled={busy}>
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Handshake size={12} />}
        {submitLabel}
      </Button>
    </div>
  );
}
