"""Security — sensitive-region detection and redaction.

Before any frame or OCR text leaves the machine (to a cloud vision model, a log
or a trajectory), the security layer can detect sensitive UI (password fields,
API keys, private keys, seed phrases, 2FA, bank/financial credentials) and either
flag the frame as sensitive (so vision routing refuses the cloud) or mask the
offending regions in the image.

Detection is deliberately conservative and content-labels based; it is ADVISORY
for routing and STRICT for redaction: if a region is flagged, masking is applied
before the image is persisted to an exportable trajectory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Bounds

# Labels that mark a control/region as sensitive.
SENSITIVE_LABELS: tuple[str, ...] = (
    "password", "passwd", "pwd", "passphrase", "api key", "apikey", "api_key",
    "secret", "token", "access token", "private key", "seed phrase", "recovery phrase",
    "mnemonic", "one-time code", "one time code", "otp", "2fa", "two-factor",
    "verification code", "security code", "cvv", "cvc", "card number",
    "account number", "routing number", "iban", "ssn", "social security",
    "pin", "credential", "sign in", "login", "tax id",
)

# Text patterns that mark a region as sensitive even without a label.
SENSITIVE_PATTERNS: tuple[str, ...] = (
    r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b",   # a card number
    r"\b[A-Z0-9]{16,}\b",                          # an API-key-shaped token
    r"\b\d{3}-\d{2}-\d{4}\b",                      # SSN
    r"\bsk-[A-Za-z0-9]{16,}\b",                    # secret key
)

_SENSITIVE_RE = re.compile("|".join(SENSITIVE_PATTERNS))


# A CAPTCHA / MFA / security challenge is the one place automation must STOP and
# ask a person. These markers are matched against window titles and on-screen text
# (case-insensitive); the same vocabulary the app adapters list as failure modes.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "captcha", "recaptcha", "hcaptcha", "i'm not a robot", "i am not a robot",
    "verify you are human", "verify you're human", "prove you are human",
    "two-factor", "two factor", "2fa", "one-time code", "one time code",
    "one-time password", "verification code", "authenticator", "6-digit code",
    "enter the code", "security check", "unusual activity", "confirm your identity",
    "touch id", "fingerprint", "face id", "biometric", "windows hello",
    "scan your fingerprint", "sign in to continue", "session expired",
)


def detect_challenge(*texts: str) -> Optional[str]:
    """The challenge marker present in ``texts`` (or None).

    A CAPTCHA/MFA/biometric prompt is where a person must take over. This only
    DETECTS; it never types into or dismisses the challenge.
    """
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return None
    for marker in CHALLENGE_MARKERS:
        if marker in haystack:
            return marker
    return None


def scan_injection(text: str) -> list[str]:
    """On-screen text that tries to pose as an instruction (advisory).

    Reuses the browser injection vocabulary so a desktop dialog cannot smuggle a
    command through a screenshot the way a web page can. The hard guarantee remains
    the UNTRUSTED wrapper + prompt; this is a second signal.
    """
    try:
        from ..browser.policy import scan_injection as _browser_scan  # noqa: PLC0415

        return _browser_scan(text)
    except Exception:  # noqa: BLE001 - browser module is optional
        return []


def is_sensitive_label(text: str) -> bool:
    lowered = (text or "").lower()
    return any(label in lowered for label in SENSITIVE_LABELS)


def looks_sensitive(text: str) -> bool:
    """Whether free text contains something that looks like a secret."""
    return bool(_SENSITIVE_RE.search(text or ""))


@dataclass
class SensitiveRegion:
    label: str
    bounds: Optional[Bounds] = None
    kind: str = "field"

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "bounds": self.bounds.to_dict() if self.bounds else None, "kind": self.kind}


@dataclass(frozen=True)
class AppProfile:
    """Per-application redaction rules.

    A generic label list cannot know that a trading terminal's "Buying Power" or
    "Net Liq" is account data, or that a password manager's entire vault view is
    secret. A profile adds labels/patterns for ONE family of apps and can mask
    every text field regardless of its label.
    """

    id: str
    app_match: tuple[str, ...] = ()
    title_match: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    mask_all_text_fields: bool = False

    def matches(self, application: str, window_title: str) -> bool:
        haystack = f"{application or ''} {window_title or ''}".lower()
        return any(text and text in haystack for text in (*self.app_match, *self.title_match))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "appMatch": list(self.app_match),
            "titleMatch": list(self.title_match), "labels": list(self.labels),
            "patterns": list(self.patterns),
            "maskAllTextFields": self.mask_all_text_fields,
        }


# The built-in profiles. Deliberately narrow: a false positive only masks a
# region that did not need masking (safe), while a false negative leaks.
DEFAULT_APP_PROFILES: tuple[AppProfile, ...] = (
    AppProfile(
        id="trading_terminal",
        app_match=("tradingview", "metatrader", "mt5", "tws", "ibkr", "trader workstation",
                   "bloomberg", "thinkorswim", "tradestation", "ninjatrader"),
        title_match=("order ticket", "order entry", "trade station", "portfolio", "account"),
        labels=("buying power", "net liq", "net liquidation", "account value", "equity",
                "balance", "cash", "margin", "position", "positions", "p&l", "pnl",
                "realized", "unrealized", "avg price", "average price", "open orders",
                "fills", "buying_power", "account number", "routing number"),
        patterns=(r"\bU\d{7,8}\b",),  # IBKR account ids
    ),
    AppProfile(
        id="broker_web",
        title_match=("schwab", "fidelity", "robinhood", "e*trade", "etrade", "merrill",
                     "vanguard", "interactive brokers"),
        labels=("buying power", "total account value", "positions", "holdings",
                "available to trade", "cash balance", "net worth"),
    ),
    AppProfile(
        id="password_manager",
        app_match=("1password", "bitwarden", "keepass", "lastpass", "dashlane"),
        title_match=("vault", "password manager"),
        labels=("vault", "password", "secret", "note"),
        mask_all_text_fields=True,
    ),
    AppProfile(
        id="banking",
        app_match=("bank", "chase", "wells fargo", "hsbc", "barclays", "revolut"),
        title_match=("online banking", "account summary"),
        labels=("available balance", "current balance", "routing", "iban", "sort code",
                "account holder", "statement"),
        patterns=(r"\b\d{8,12}\b",),
    ),
)


@dataclass
class SecurityReport:
    sensitive: bool = False
    regions: list[SensitiveRegion] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensitive": self.sensitive,
            "regions": [r.to_dict() for r in self.regions],
            "reasons": self.reasons,
        }


class SecurityLayer:
    def __init__(self, *, redaction_enabled: bool = True,
                 profiles: Optional[list[AppProfile]] = None) -> None:
        self.redaction_enabled = redaction_enabled
        self.profiles: list[AppProfile] = list(profiles) if profiles is not None else list(DEFAULT_APP_PROFILES)

    def select_profile(self, *, application: str = "", window_title: str = "") -> Optional[AppProfile]:
        for profile in self.profiles:
            if profile.matches(application, window_title):
                return profile
        return None

    def scan_snapshot(self, elements: list[dict], *, text: str = "",
                      application: str = "", window_title: str = "") -> SecurityReport:
        """Flag sensitive regions from snapshot elements + rendered text.

        A matching application profile widens the label/pattern vocabulary for
        that window and may mask every text field (a password manager).
        """
        regions: list[SensitiveRegion] = []
        reasons: list[str] = []
        profile = self.select_profile(application=application, window_title=window_title)
        extra_labels = tuple(profile.labels) if profile else ()
        extra_patterns = tuple(profile.patterns) if profile else ()
        for element in elements:
            label = str(element.get("name") or "") + " " + str(element.get("role") or "")
            role = str(element.get("role") or "").lower()
            profile_hit = any(extra and extra in label.lower() for extra in extra_labels)
            mask_all = bool(profile and profile.mask_all_text_fields
                            and any(word in role for word in ("textbox", "edit", "text field", "password")))
            if is_sensitive_label(label) or profile_hit or mask_all:
                regions.append(SensitiveRegion(
                    label=str(element.get("name") or element.get("role") or "sensitive"),
                    bounds=Bounds.from_any(element.get("bounds")), kind="field",
                ))
                if profile_hit:
                    reasons.append(f"{profile.id} control labelled {element.get('name')!r}")
                elif mask_all:
                    reasons.append(f"{profile.id} text field masked wholesale")
                else:
                    reasons.append(f"control labelled {element.get('name')!r}")
        if text and looks_sensitive(text):
            reasons.append("text matches a secret pattern")
        if text and extra_patterns:
            for pattern in extra_patterns:
                try:
                    if re.search(pattern, text):
                        reasons.append(f"{profile.id if profile else 'app'} text matches a profile pattern")
                        break
                except re.error:
                    continue
        sensitive = bool(regions) or bool(reasons)
        return SecurityReport(sensitive=sensitive, regions=regions, reasons=reasons)

    def mask_image(self, image: Any, regions: list[SensitiveRegion], *,
                   origin: Optional[tuple[int, int]] = None, scale: float = 1.0) -> Any:
        """Black out sensitive regions in a PIL image (best-effort).

        `origin` is the image's top-left in SCREEN coordinates and `scale` maps
        screen pixels to image pixels. Element bounds are screen coordinates; a
        window capture is window-local, so subtracting the origin is required or
        the mask lands on the wrong pixels (and the secret stays visible).
        """
        if not self.redaction_enabled or image is None or not regions:
            return image
        try:
            from PIL import ImageDraw  # noqa: PLC0415

            masked = image.copy()
            draw = ImageDraw.Draw(masked)
            ox, oy = origin or (0, 0)
            factor = float(scale or 1.0) or 1.0
            for region in regions:
                if region.bounds is None:
                    continue
                try:
                    left = (region.bounds.x - ox) * factor
                    top = (region.bounds.y - oy) * factor
                    right = (region.bounds.right - ox) * factor
                    bottom = (region.bounds.bottom - oy) * factor
                    if right <= left or bottom <= top:
                        continue
                    draw.rectangle([left, top, right, bottom], fill=(0, 0, 0))
                except Exception:  # noqa: BLE001 - one bad region must not skip the rest
                    continue
            return masked
        except Exception:  # noqa: BLE001
            return image

    def redact_text(self, text: str) -> str:
        if not self.redaction_enabled:
            return text
        return _SENSITIVE_RE.sub("«redacted»", text or "")


__all__ = [
    "CHALLENGE_MARKERS",
    "DEFAULT_APP_PROFILES",
    "SENSITIVE_LABELS",
    "SENSITIVE_PATTERNS",
    "AppProfile",
    "SecurityLayer",
    "SecurityReport",
    "SensitiveRegion",
    "detect_challenge",
    "is_sensitive_label",
    "looks_sensitive",
    "scan_injection",
]
