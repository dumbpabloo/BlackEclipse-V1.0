"""Optional segregation-of-duties (SoD) policies for finance effects.

This is a SECOND, independent gate above the existing single-use approval layer.
It is **off by default**: with no policy configured, `check()` returns satisfied
and the single-user local flow is unchanged. When an operator configures a
policy, an effect above the threshold needs N DISTINCT approvers (and, by
default, none of them may be the requester) in addition to the normal single-use
approval.

Policies are per effect (`ledger.write`, `portfolio.write`,
`portfolio.rebalance_proposal`). Sign-offs are keyed by the exact call digest, so
changing the amount/arguments needs fresh sign-offs. Sign-offs are consumed when
the call is authorized, so one set cannot authorize a second call.

Deliberate split of authority:
  * the AGENT cannot sign off (there is no SoD tool that records an approval);
  * an operator/UI route records a sign-off, and
  * the authorizer only READS the collected sign-offs and consumes them.

`agent-memory.db` holds no secret material — effect names, digests and approver
identity only.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .persistence import now_iso


class SoDError(Exception):
    def __init__(self, code: str, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0").replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def _args_amount(args: dict[str, Any]) -> Decimal:
    """Best-effort monetary amount of an effectful call, never negative.

    Order matters: explicit totals first, then a balanced journal entry's debit
    total, then quantity × price. Unknown shapes yield 0, which can only make the
    policy LESS restrictive at a zero threshold — a policy author should set a
    threshold above 0 for amount-based control.
    """
    for key in ("amount", "total", "net_amount", "netAmount", "gross_amount",
                "grossAmount", "value", "notional"):
        if args.get(key) not in (None, ""):
            return abs(_dec(args.get(key)))
    lines = args.get("lines")
    if isinstance(lines, list) and lines:
        debit = sum((_dec(line.get("debit")) for line in lines if isinstance(line, dict)), Decimal("0"))
        credit = sum((_dec(line.get("credit")) for line in lines if isinstance(line, dict)), Decimal("0"))
        return max(debit, credit)
    quantity = args.get("quantity")
    price = args.get("price")
    if quantity not in (None, "") and price not in (None, ""):
        return abs(_dec(quantity) * _dec(price))
    return Decimal("0")


class SoDService:
    """Policy + sign-off store on the ONE agent-memory connection."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._ensure_schema()

    @property
    def _db(self):
        return self._store._db  # noqa: SLF001 - the shared connection

    def _ensure_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS SoDPolicy (
              id                 TEXT PRIMARY KEY,
              effect             TEXT NOT NULL,
              thresholdAmount    TEXT NOT NULL DEFAULT '0',
              requiredApprovals  INTEGER NOT NULL DEFAULT 2,
              separateRequester  INTEGER NOT NULL DEFAULT 1,
              active             INTEGER NOT NULL DEFAULT 1,
              createdBy          TEXT,
              createdAt          TEXT NOT NULL,
              updatedAt          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sod_policy_effect ON SoDPolicy(effect, active);

            CREATE TABLE IF NOT EXISTS SoDSignoff (
              id          TEXT PRIMARY KEY,
              effect      TEXT NOT NULL,
              digest      TEXT NOT NULL,
              approver    TEXT NOT NULL,
              amount      TEXT,
              createdAt   TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_sod_signoff
              ON SoDSignoff(effect, digest, approver);
            CREATE INDEX IF NOT EXISTS idx_sod_signoff_digest
              ON SoDSignoff(effect, digest);
            """
        )
        self._db.commit()

    # ── policy ──────────────────────────────────────────────────────────────
    def _policy_row(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "effect": row["effect"],
            "thresholdAmount": row["thresholdAmount"],
            "requiredApprovals": int(row["requiredApprovals"]),
            "separateRequester": bool(row["separateRequester"]),
            "active": bool(row["active"]),
            "createdBy": row["createdBy"],
            "createdAt": row["createdAt"],
            "updatedAt": row["updatedAt"],
        }

    def set_policy(
        self,
        effect: str,
        *,
        threshold_amount: Any = 0,
        required_approvals: int = 2,
        separate_requester: bool = True,
        actor: Optional[str] = None,
    ) -> dict[str, Any]:
        effect = str(effect or "").strip()
        if not effect:
            raise SoDError("SOD_EFFECT_REQUIRED", "`effect` is required")
        threshold = _dec(threshold_amount)
        if threshold < 0:
            raise SoDError("SOD_INVALID", "threshold cannot be negative")
        if int(required_approvals) < 1:
            raise SoDError("SOD_INVALID", "required_approvals must be at least 1")
        now = now_iso()
        policy_id = f"sod_{uuid.uuid4().hex[:20]}"
        # A new policy at the same effect+threshold supersedes the old one.
        self._db.execute(
            "UPDATE SoDPolicy SET active=0, updatedAt=? WHERE effect=? AND thresholdAmount=? AND active=1",
            (now, effect, _money(threshold)),
        )
        self._db.execute(
            """INSERT INTO SoDPolicy
               (id, effect, thresholdAmount, requiredApprovals, separateRequester,
                active, createdBy, createdAt, updatedAt)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (policy_id, effect, _money(threshold), int(required_approvals),
             1 if separate_requester else 0, actor, now, now),
        )
        self._db.commit()
        row = self._db.execute("SELECT * FROM SoDPolicy WHERE id=?", (policy_id,)).fetchone()
        return self._policy_row(row)

    def list_policies(self, effect: Optional[str] = None,
                      *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM SoDPolicy"
        clauses = []
        args: list[Any] = []
        if effect:
            clauses.append("effect=?")
            args.append(str(effect))
        if active_only:
            clauses.append("active=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY effect, CAST(thresholdAmount AS REAL) DESC"
        return [self._policy_row(row) for row in self._db.execute(sql, tuple(args)).fetchall()]

    def deactivate_policy(self, policy_id: str) -> bool:
        cursor = self._db.execute(
            "UPDATE SoDPolicy SET active=0, updatedAt=? WHERE id=? AND active=1",
            (now_iso(), str(policy_id)),
        )
        self._db.commit()
        return bool(cursor.rowcount)

    def effective_policy(self, effect: str, amount: Decimal) -> Optional[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM SoDPolicy WHERE effect=? AND active=1", (str(effect),)
        ).fetchall()
        candidates = [self._policy_row(row) for row in rows
                      if _dec(row["thresholdAmount"]) <= amount]
        if not candidates:
            return None
        return max(candidates, key=lambda row: _dec(row["thresholdAmount"]))

    # ── sign-offs / status ──────────────────────────────────────────────────
    def _approvers(self, effect: str, digest: str) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT approver FROM SoDSignoff WHERE effect=? AND digest=? ORDER BY createdAt",
            (str(effect), str(digest)),
        ).fetchall()
        return [row["approver"] for row in rows]

    def check(self, effect: str, amount: Any, digest: str,
              requester: Optional[str] = None) -> dict[str, Any]:
        value = _dec(amount)
        policy = self.effective_policy(effect, value)
        if policy is None:
            return {
                "effect": effect, "digest": digest, "policy": None,
                "required": 0, "collected": [], "remaining": 0,
                "satisfied": True, "amount": _money(value),
            }
        approvers = self._approvers(effect, digest)
        excluded = []
        if policy["separateRequester"] and requester:
            excluded = [a for a in approvers if a == requester]
            approvers = [a for a in approvers if a != requester]
        required = int(policy["requiredApprovals"])
        return {
            "effect": effect, "digest": digest,
            "policy": policy,
            "amount": _money(value),
            "required": required,
            "collected": approvers,
            "excludedSelfApprovals": excluded,
            "remaining": max(0, required - len(approvers)),
            "satisfied": len(approvers) >= required,
        }

    def sign_off(self, effect: str, digest: str, approver: str, *,
                 amount: Any = None, requester: Optional[str] = None) -> dict[str, Any]:
        effect = str(effect or "").strip()
        digest = str(digest or "").strip()
        approver = str(approver or "").strip()
        if not effect or not digest:
            raise SoDError("SOD_INVALID", "`effect` and `digest` are required")
        if not approver:
            raise SoDError("SOD_APPROVER_REQUIRED", "`approver` is required")
        value = _dec(amount)
        policy = self.effective_policy(effect, value)
        if policy is None:
            return {"status": "NO_POLICY", "required": 0, "satisfied": True,
                    "effect": effect, "digest": digest}
        if policy["separateRequester"] and requester and approver == requester:
            raise SoDError(
                "SOD_SELF_APPROVAL",
                "the requester cannot satisfy their own segregation-of-duties policy",
                {"effect": effect, "approver": approver},
            )
        try:
            self._db.execute(
                """INSERT INTO SoDSignoff (id, effect, digest, approver, amount, createdAt)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (f"sod_sig_{uuid.uuid4().hex[:20]}", effect, digest, approver,
                 _money(value), now_iso()),
            )
        except Exception:  # noqa: BLE001 - duplicate approver is idempotent
            pass
        self._db.commit()
        status = self.check(effect, value, digest, requester=None)
        return {"status": "PENDING" if not status["satisfied"] else "SATISFIED", **status}

    def consume(self, effect: str, digest: str) -> int:
        cursor = self._db.execute(
            "DELETE FROM SoDSignoff WHERE effect=? AND digest=?",
            (str(effect), str(digest)),
        )
        self._db.commit()
        return int(cursor.rowcount or 0)

    def list_signoffs(self, effect: str, digest: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM SoDSignoff WHERE effect=? AND digest=? ORDER BY createdAt",
            (str(effect), str(digest)),
        ).fetchall()
        return [{
            "id": row["id"], "effect": row["effect"], "digest": row["digest"],
            "approver": row["approver"], "amount": row["amount"],
            "createdAt": row["createdAt"],
        } for row in rows]


def effect_amount(tool_name: str, args: dict[str, Any]) -> Decimal:
    """Public helper so authorizers compute the same amount the policy sees."""
    return _args_amount(args or {})


__all__ = ["SoDError", "SoDService", "effect_amount", "_args_amount"]
