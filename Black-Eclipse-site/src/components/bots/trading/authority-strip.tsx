"use client";

// ── Bot authority strip ─────────────────────────────────────────────────────
// Shows the live arm and the mandate the runner will actually use. Auto mode
// renews both at the next entry; this is the countdown, not a second scheduler.

import { useState } from "react";
import { renewBotTradingAuthority, type BotTradingAuthorityView } from "@/lib/harness/clients/automation-browser";
import { cn } from "@/lib/utils";

export function AuthorityStrip({
  botId,
  authority,
  disabled,
  onRenewed,
}: {
  botId: string;
  authority: BotTradingAuthorityView;
  disabled: boolean;
  onRenewed: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const renew = async () => {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const result = await renewBotTradingAuthority(botId);
      setNotice(
        result.renewed || result.arm?.renewed
          ? "Authority renewed."
          : result.arm?.message ?? "Nothing needed renewal.",
      );
      await onRenewed();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not renew authority");
    } finally {
      setBusy(false);
    }
  };

  const armExpired = authority.mode === "live" && (authority.armSecondsLeft ?? 0) <= 0;
  return (
    <div className="rounded-lg border border-border/15 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold">Authority</div>
        <button
          type="button"
          disabled={busy || disabled}
          onClick={() => void renew()}
          className="rounded px-2 py-1 text-[10px] font-semibold text-sky-300 hover:bg-sky-500/10 disabled:opacity-50"
        >
          {busy ? "Renewing…" : "Renew now"}
        </button>
      </div>
      <div className="mt-1 grid grid-cols-2 gap-2 text-[10px] sm:grid-cols-3">
        {authority.mode === "live" && (
          <div>
            <p className="text-[9px] uppercase tracking-wide text-muted-foreground">Live arm</p>
            <p className={cn("font-mono", armExpired ? "text-rose-300" : "text-foreground")}>
              {remainingLabel(authority.armSecondsLeft)}
            </p>
          </div>
        )}
        <div>
          <p className="text-[9px] uppercase tracking-wide text-muted-foreground">Mandate</p>
          <p className="font-mono text-foreground">{remainingLabel(authority.mandateSecondsLeft)}</p>
        </div>
        <div>
          <p className="text-[9px] uppercase tracking-wide text-muted-foreground">Orders left</p>
          <p className="font-mono text-foreground">
            {authority.ordersRemaining ?? "—"}
            {authority.maxOrders != null ? ` / ${authority.maxOrders}` : ""}
          </p>
        </div>
      </div>
      <p className="mt-1 text-[10px] text-muted-foreground">
        {armExpired
          ? "The live arm expired. Arm the connection again; auto-renew never resurrects an expired arm."
          : authority.needsMandateRenewal
            ? "The mandate is due. The next entry renews it automatically, clamped to the current stage."
            : "Auto-renew keeps this alive at the next entry while the standing consent is valid."}
      </p>
      {notice && <p className="mt-1 text-[10px] text-emerald-300">{notice}</p>}
      {error && <p className="mt-1 text-[10px] text-rose-300">{error}</p>}
    </div>
  );
}

function remainingLabel(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds <= 0) return "expired";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours >= 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}
