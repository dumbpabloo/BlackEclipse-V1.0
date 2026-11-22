"""The Channel Gateway provider registry.

One place to declare a provider's operator-visible contract and how to build
its transport. Keep provider modules free of registry knowledge: they expose a
class, the registry decides when it applies.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .provider import ConfigField, ProviderContext, ProviderSpec

log = logging.getLogger("harness.channels.registry")


_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        kind="telegram",
        label="Telegram",
        description="Bot long-poll inbound and sendMessage outbound. No public URL required.",
        inbound_mode="poll",
        config_fields=(
            ConfigField("botToken", "Bot token", secret=True, required=True, placeholder="123456:ABC-DEF."),
        ),
    ),
    ProviderSpec(
        kind="wecom",
        label="WeCom (WeChat Work)",
        description="Group-robot alerts over the webhook API. Outbound only.",
        inbound_mode="none",
        config_fields=(
            ConfigField("webhookKey", "Robot key", secret=True, required=True,
                        hint="From the group chat's robot settings"),
        ),
    ),
    ProviderSpec(
        kind="viber",
        label="Viber",
        description="Signed webhook inbound (hex body HMAC) and send_message outbound.",
        inbound_mode="push",
        capabilities=frozenset({"media", "stickers"}),
        config_fields=(
            ConfigField("authToken", "Auth token", secret=True, required=True,
                        hint="Signs inbound bodies and authenticates sends"),
            ConfigField("senderName", "Sender name", placeholder="Eclipse"),
        ),
    ),
    ProviderSpec(
        kind="line",
        label="LINE",
        description="Signed webhook inbound (body HMAC) and Messaging API push outbound.",
        inbound_mode="push",
        capabilities=frozenset({"stickers", "media", "groups"}),
        config_fields=(
            ConfigField("channelAccessToken", "Channel access token", secret=True, required=True),
            ConfigField("channelSecret", "Channel secret", secret=True, required=True,
                        hint="Signs inbound bodies (X-Line-Signature)"),
        ),
    ),
    ProviderSpec(
        kind="twilio",
        label="Twilio (SMS)",
        description="Signed webhook inbound and REST outbound for SMS.",
        inbound_mode="push",
        capabilities=frozenset({"sms", "media"}),
        config_fields=(
            ConfigField("accountSid", "Account SID", required=True, placeholder="AC..."),
            ConfigField("authToken", "Auth token", secret=True, required=True,
                        hint="Signs inbound webhooks and authenticates outbound"),
            ConfigField("fromNumber", "From number", required=True, placeholder="+15550001111"),
            ConfigField("publicUrl", "Public webhook URL", required=True,
                        hint="The exact URL Twilio signs, e.g. https://app.example.com/api/channels/webhook/twilio"),
        ),
    ),
    ProviderSpec(
        kind="signal",
        label="Signal",
        description="Send-only alerts through a self-hosted signal-cli bridge. No inbound.",
        inbound_mode="none",
        config_fields=(
            ConfigField("baseUrl", "signal-cli REST URL", required=True, placeholder="http://127.0.0.1:8080"),
            ConfigField("number", "Bot Signal number", required=True, placeholder="+15550001111"),
            ConfigField("apiToken", "Bridge API token", secret=True, hint="Only when the bridge sits behind an authenticating proxy"),
        ),
    ),
    ProviderSpec(
        kind="rocketchat",
        label="Rocket.Chat",
        description="Channel-history poll inbound and chat.postMessage sends. No public URL required.",
        inbound_mode="poll",
        capabilities=frozenset({"threads", "files", "reactions"}),
        config_fields=(
            ConfigField("baseUrl", "Rocket.Chat URL", required=True, placeholder="https://chat.example.com"),
            ConfigField("authToken", "Personal access token", secret=True, required=True),
            ConfigField("userId", "Bot user ID", required=True, hint="Sent as X-User-Id and used to ignore its own posts"),
            ConfigField("roomId", "Room ID", hint="Required for inbound: the room to watch"),
            ConfigField("pollSeconds", "Poll interval (seconds)", placeholder="5"),
        ),
    ),
    ProviderSpec(
        kind="zulip",
        label="Zulip",
        description="Event-queue long-poll inbound and stream sends. No public URL required.",
        inbound_mode="poll",
        capabilities=frozenset({"streams", "topics", "files"}),
        config_fields=(
            ConfigField("site", "Zulip site URL", required=True, placeholder="https://chat.example.com"),
            ConfigField("botEmail", "Bot email", required=True, placeholder="eclipse-bot@example.com"),
            ConfigField("apiKey", "Bot API key", secret=True, required=True),
            ConfigField("defaultStream", "Default stream"),
            ConfigField("topic", "Reply topic", placeholder="eclipse"),
            ConfigField("pollSeconds", "Event poll window (seconds)", placeholder="30"),
        ),
    ),
    ProviderSpec(
        kind="matrix",
        label="Matrix",
        description="/sync long-poll inbound and room sends. No public URL required.",
        inbound_mode="poll",
        capabilities=frozenset({"threads", "files", "e2ee_rooms"}),
        config_fields=(
            ConfigField("homeserverUrl", "Homeserver URL", required=True, placeholder="https://matrix.example.org"),
            ConfigField("accessToken", "Access token", secret=True, required=True),
            ConfigField("userId", "Bot user ID", required=True, placeholder="@eclipse:example.org",
                        hint="Required so the bot ignores its own messages"),
            ConfigField("defaultRoomId", "Default room ID", placeholder="!abc:example.org"),
            ConfigField("pollTimeoutMs", "Sync timeout (ms)", placeholder="30000"),
        ),
    ),
    ProviderSpec(
        kind="discord",
        label="Discord",
        description="Gateway WebSocket inbound and REST outbound. No public URL required.",
        inbound_mode="poll",
        config_fields=(
            ConfigField("botToken", "Bot token", secret=True, required=True),
        ),
    ),
    ProviderSpec(
        kind="email",
        label="Email",
        description="IMAP inbox polling and SMTP replies. No public URL required.",
        inbound_mode="poll",
        config_fields=(
            ConfigField("imapHost", "IMAP host", required=True),
            ConfigField("imapUser", "IMAP username"),
            ConfigField("imapPass", "IMAP password", secret=True),
            ConfigField("imapPort", "IMAP port", placeholder="993"),
            ConfigField("smtpHost", "SMTP host"),
            ConfigField("smtpPort", "SMTP port", placeholder="587"),
            ConfigField("smtpUser", "SMTP username"),
            ConfigField("smtpPass", "SMTP password", secret=True),
            ConfigField("fromAddress", "From address"),
            ConfigField("pollSeconds", "Poll interval (seconds)", placeholder="60"),
        ),
    ),
    ProviderSpec(
        kind="slack",
        label="Slack",
        description="Socket Mode (no public URL) or Events webhook inbound; Web API outbound.",
        inbound_mode="both",
        capabilities=frozenset({"threads", "reactions", "files"}),
        config_fields=(
            ConfigField("botToken", "Bot token", secret=True, required=True, placeholder="xoxb-..."),
            ConfigField("signingSecret", "Signing secret", secret=True, hint="Required for the HTTP webhook path"),
            ConfigField("appToken", "App-level token", secret=True, placeholder="xapp-..."),
            ConfigField("defaultChannel", "Default channel ID"),
        ),
    ),
    ProviderSpec(
        kind="whatsapp",
        label="WhatsApp",
        description="Meta WhatsApp Cloud API webhook inbound and Graph API outbound.",
        inbound_mode="push",
        capabilities=frozenset({"templates", "media"}),
        config_fields=(
            ConfigField("accessToken", "Access token", secret=True, required=True),
            ConfigField("phoneNumberId", "Phone number ID", required=True),
            ConfigField("appSecret", "App secret", secret=True, hint="Required for inbound signature verification"),
            ConfigField("verifyToken", "Webhook verify token", secret=True, hint="Required for the subscription handshake"),
            ConfigField("apiVersion", "Graph API version", placeholder="v21.0"),
        ),
    ),
    ProviderSpec(
        kind="teams",
        label="Microsoft Teams",
        description="Bot Framework webhook inbound and connector outbound.",
        inbound_mode="push",
        capabilities=frozenset({"threads", "cards"}),
        config_fields=(
            ConfigField("appId", "Bot app ID", required=True),
            ConfigField("appPassword", "Bot app password", secret=True, required=True),
            ConfigField("tenantId", "Tenant ID", placeholder="common"),
            ConfigField("defaultConversationId", "Default conversation ID"),
            ConfigField("defaultServiceUrl", "Default service URL", placeholder="https://smba.trafficmanager.net/teams"),
        ),
    ),
    ProviderSpec(
        kind="google_chat",
        label="Google Chat",
        description="Chat app webhook inbound and Chat API outbound via service account or ADC.",
        inbound_mode="push",
        capabilities=frozenset({"threads", "cards"}),
        any_required=(("credentialsJson",), ("useAdc",)),
        flag_fields=frozenset({"useAdc"}),
        config_fields=(
            ConfigField("credentialsJson", "Service-account JSON", secret=True, hint="Inline JSON or a key-file path"),
            ConfigField("useAdc", "Use Application Default Credentials", placeholder="true"),
            ConfigField("projectNumber", "Google Cloud project number", hint="Required to verify inbound webhooks"),
            ConfigField("defaultSpaceId", "Default space ID", placeholder="spaces/AAAA..."),
            ConfigField("pubsubAudience", "Pub/Sub audience", hint="Alternative to project number for Pub/Sub delivery"),
        ),
    ),
    ProviderSpec(
        kind="lark",
        label="Lark / Feishu",
        description="Lark WebSocket (no public URL) or event webhook inbound; IM API outbound.",
        inbound_mode="both",
        capabilities=frozenset({"threads", "rich_text", "reactions"}),
        config_fields=(
            ConfigField("appId", "App ID", required=True),
            ConfigField("appSecret", "App secret", secret=True, required=True),
            ConfigField("defaultChatId", "Default chat ID"),
            ConfigField("verificationToken", "Event verification token", secret=True),
            ConfigField("encryptKey", "Event encrypt key", secret=True),
            ConfigField("domain", "Domain", placeholder="open.feishu.cn"),
            ConfigField("useWebsocket", "Use WebSocket long connection", placeholder="true"),
        ),
    ),
    ProviderSpec(
        kind="webhook",
        label="Webhook",
        description="Generic signed webhook inbound and one authenticated HTTP POST outbound.",
        inbound_mode="push",
        config_fields=(
            ConfigField("replyUrl", "Reply URL", required=True),
            ConfigField("replyToken", "Reply bearer token", secret=True),
            ConfigField("webhookSecret", "Inbound signing secret", secret=True),
        ),
    ),
    ProviderSpec(
        kind="dingtalk",
        label="DingTalk",
        description=(
            "Outbound-only DingTalk custom-robot webhook with HMAC signing "
            "(alerts and scheduled briefs; no inbound)."
        ),
        inbound_mode="none",
        capabilities=frozenset({"send"}),
        config_fields=(
            ConfigField("webhookUrl", "Robot webhook URL", secret=True, required=True),
            ConfigField("signSecret", "Sign secret", secret=True,
                        hint="Optional; without it the robot accepts unsigned posts"),
        ),
    ),
    ProviderSpec(
        kind="mattermost",
        label="Mattermost",
        description=(
            "Outbound-only posts to a self-hosted Mattermost server over its REST "
            "API (alerts and scheduled briefs; no inbound)."
        ),
        inbound_mode="none",
        capabilities=frozenset({"send"}),
        config_fields=(
            ConfigField("baseUrl", "Server URL", required=True,
                        placeholder="https://chat.example.com"),
            ConfigField("botToken", "Bot access token", secret=True, required=True),
            ConfigField("channelId", "Default channel id", required=True),
        ),
    ),
)

PROVIDERS: dict[str, ProviderSpec] = {spec.kind: spec for spec in _SPECS}
KINDS: tuple[str, ...] = tuple(PROVIDERS)
PUSH_KINDS: frozenset[str] = frozenset(
    k for k, s in PROVIDERS.items() if s.inbound_mode in ("push", "both")
)
POLL_KINDS: frozenset[str] = frozenset(
    k for k, s in PROVIDERS.items() if s.inbound_mode in ("poll", "both")
)
#: Providers that only SEND (an alerts/notification surface). ``PUSH_KINDS`` and
#: ``POLL_KINDS`` are CAPABILITY sets, not a partition: a ``both`` provider (e.g.
#: lark) appears in both. The only exclusivity is that an outbound-only provider is
#: in neither, and a push/poll provider is in at least one.
OUTBOUND_KINDS: frozenset[str] = frozenset(
    k for k, s in PROVIDERS.items() if s.inbound_mode == "none"
)


def spec_for(kind: str) -> Optional[ProviderSpec]:
    return PROVIDERS.get(kind)


def describe(config_names: Any = ()) -> dict[str, dict[str, Any]]:
    """Every provider plus any *configured* name, including ones we do not know.

    Parity with Vibe-Trading's `channels/registry.py::inspect_channels`: the status
    surface unions the built-in names with the names present in the operator's
    config, so a channel that was configured but never wired up is **visible as
    unknown** instead of silently absent. That matters: silently dropping a
    configured name is how an operator ends up believing a channel is on when it is
    not.

    ``known`` is the field that matters: ``configured and not known`` means the
    config names a provider this build does not implement.
    """
    names = set(PROVIDERS) | {str(name) for name in config_names if name}
    configured = {str(name) for name in config_names if name}
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        spec = PROVIDERS.get(name)
        if spec is None:
            out[name] = {
                "kind": name,
                "known": False,
                "label": name,
                "inboundMode": "none",
                "configured": True,
            }
            continue
        out[name] = {
            "kind": name,
            "known": True,
            "label": spec.label,
            "inboundMode": spec.inbound_mode,
            "configured": name in configured,
        }
    return out


def is_configured(kind: str, get: Any) -> bool:
    spec = PROVIDERS.get(kind)
    return bool(spec and spec.is_configured(get))


def build_provider(ctx: ProviderContext) -> Any:
    """Construct a transport for `ctx.kind`, or None when it is incomplete.

    A missing required credential is an honest None: the channel is simply not
    running. Import provider modules lazily so the registry stays cheap and a
    missing optional dependency never blocks boot.
    """
    kind = ctx.kind
    cfg = ctx.config

    def _s(key: str) -> str:
        return ctx.secret(key)

    if kind == "telegram":
        token = _s("botToken")
        if not token:
            log.info("telegram channel enabled but no bot token configured")
            return None
        from .providers.telegram import TelegramProvider  # noqa: PLC0415

        return TelegramProvider(bot_token=token)

    if kind == "matrix":
        base, token, user = _s("homeserverUrl"), _s("accessToken"), _s("userId")
        if not base or not token or not user:
            log.info("matrix channel enabled but homeserverUrl/accessToken/userId is missing")
            return None
        from .providers.matrix import MatrixProvider  # noqa: PLC0415

        return MatrixProvider(
            homeserver_url=base,
            access_token=token,
            user_id=user,
            default_room_id=_s("defaultRoomId"),
            poll_timeout_ms=int(cfg.get("pollTimeoutMs") or 30000),
        )

    if kind == "zulip":
        site, email, key = _s("site"), _s("botEmail"), _s("apiKey")
        if not site or not email or not key:
            log.info("zulip channel enabled but site/botEmail/apiKey is missing")
            return None
        from .providers.zulip import ZulipProvider  # noqa: PLC0415

        return ZulipProvider(
            site=site,
            bot_email=email,
            api_key=key,
            default_stream=_s("defaultStream"),
            topic=str(cfg.get("topic") or "eclipse"),
            poll_seconds=int(cfg.get("pollSeconds") or 30),
        )

    if kind == "rocketchat":
        base, token, user = _s("baseUrl"), _s("authToken"), _s("userId")
        if not base or not token or not user:
            log.info("rocketchat channel enabled but baseUrl/authToken/userId is missing")
            return None
        from .providers.rocketchat import RocketChatProvider  # noqa: PLC0415

        return RocketChatProvider(
            base_url=base,
            auth_token=token,
            user_id=user,
            room_id=_s("roomId"),
            poll_seconds=int(cfg.get("pollSeconds") or 5),
        )

    if kind == "wecom":
        key = _s("webhookKey")
        if not key:
            log.info("wecom channel enabled but no robot key configured")
            return None
        from .providers.wecom import WecomProvider  # noqa: PLC0415

        return WecomProvider(webhook_key=key)

    if kind == "viber":
        token = _s("authToken")
        if not token:
            log.info("viber channel enabled but no auth token configured")
            return None
        from .providers.viber import ViberProvider  # noqa: PLC0415

        return ViberProvider(auth_token=token,
                            sender_name=str(cfg.get("senderName") or "Eclipse"))

    if kind == "line":
        token, secret = _s("channelAccessToken"), _s("channelSecret")
        if not token or not secret:
            log.info("line channel enabled but channelAccessToken/channelSecret is missing")
            return None
        from .providers.line import LineProvider  # noqa: PLC0415

        return LineProvider(channel_access_token=token, channel_secret=secret)

    if kind == "twilio":
        sid, token, sender = _s("accountSid"), _s("authToken"), _s("fromNumber")
        if not sid or not token or not sender:
            log.info("twilio channel enabled but accountSid/authToken/fromNumber is missing")
            return None
        from .providers.twilio import TwilioProvider  # noqa: PLC0415

        return TwilioProvider(
            account_sid=sid,
            auth_token=token,
            from_number=sender,
            public_url=_s("publicUrl"),
        )

    if kind == "signal":
        base, number = _s("baseUrl"), _s("number")
        if not base or not number:
            log.info("signal channel enabled but baseUrl/number is missing")
            return None
        from .providers.signal import SignalProvider  # noqa: PLC0415

        return SignalProvider(
            base_url=base,
            number=number,
            api_token=_s("apiToken"),
        )

    if kind == "discord":
        token = _s("botToken")
        if not token:
            log.info("discord channel enabled but no bot token configured")
            return None
        from .providers.discord import DiscordProvider  # noqa: PLC0415

        return DiscordProvider(bot_token=token)

    if kind == "email":
        host = _s("imapHost")
        if not host:
            log.info("email channel enabled but no IMAP host configured")
            return None
        from .providers.email import EmailProvider  # noqa: PLC0415

        return EmailProvider(
            imap_host=host,
            imap_user=_s("imapUser"),
            imap_pass=_s("imapPass"),
            imap_port=int(cfg.get("imapPort") or 993),
            smtp_host=_s("smtpHost"),
            smtp_port=int(cfg.get("smtpPort") or 587),
            smtp_user=_s("smtpUser"),
            smtp_pass=_s("smtpPass"),
            from_address=_s("fromAddress"),
            poll_seconds=int(cfg.get("pollSeconds") or 60),
        )

    if kind == "slack":
        token = _s("botToken")
        if not token:
            log.info("slack channel enabled but no bot token configured")
            return None
        from .providers.slack import SlackProvider  # noqa: PLC0415

        return SlackProvider(
            bot_token=token,
            app_token=_s("appToken"),
            signing_secret=_s("signingSecret"),
            default_channel=str(cfg.get("defaultChannel") or ""),
        )

    if kind == "whatsapp":
        token, phone = _s("accessToken"), _s("phoneNumberId")
        if not token or not phone:
            log.info("whatsapp channel enabled but no access token / phone number id configured")
            return None
        from .providers.webhooks import WhatsappProvider  # noqa: PLC0415

        return WhatsappProvider(
            access_token=token,
            phone_number_id=phone,
            api_version=str(cfg.get("apiVersion") or "v21.0"),
        )

    if kind == "teams":
        app_id, app_password = _s("appId"), _s("appPassword")
        if not app_id or not app_password:
            log.info("teams channel enabled but no app id / app password configured")
            return None
        from .providers.teams import TeamsProvider  # noqa: PLC0415

        return TeamsProvider(
            app_id=app_id,
            app_password=app_password,
            tenant_id=str(cfg.get("tenantId") or "common"),
            default_conversation_id=_s("defaultConversationId"),
            default_service_url=str(cfg.get("defaultServiceUrl") or ""),
        )

    if kind == "google_chat":
        creds = _s("credentialsJson")
        use_adc = str(cfg.get("useAdc") or "").strip().lower() in ("1", "true", "yes", "on")
        if not creds and not use_adc:
            log.info("google_chat channel enabled but no credentials JSON / ADC configured")
            return None
        from .providers.google_chat import GoogleChatProvider  # noqa: PLC0415

        return GoogleChatProvider(
            credentials_json=creds,
            use_adc=use_adc,
            default_space_id=_s("defaultSpaceId"),
        )

    if kind == "lark":
        app_id, app_secret = _s("appId"), _s("appSecret")
        if not app_id or not app_secret:
            log.info("lark channel enabled but no app id / app secret configured")
            return None
        from .providers.lark import LarkProvider  # noqa: PLC0415

        return LarkProvider(
            app_id=app_id,
            app_secret=app_secret,
            default_chat_id=_s("defaultChatId"),
            domain=str(cfg.get("domain") or "open.feishu.cn"),
            use_websocket=str(cfg.get("useWebsocket") or "true").strip().lower()
            not in ("0", "false", "no", "off"),
        )

    if kind == "webhook":
        url = _s("replyUrl")
        if not url:
            log.info("webhook channel enabled but no reply URL configured")
            return None
        from .providers.webhooks import WebhookProvider  # noqa: PLC0415

        return WebhookProvider(reply_url=url, token=_s("replyToken"))

    if kind == "dingtalk":
        url = _s("webhookUrl")
        if not url:
            log.info("dingtalk channel enabled but no webhook URL configured")
            return None
        from .providers.dingtalk import DingTalkProvider  # noqa: PLC0415

        return DingTalkProvider(webhook_url=url, sign_secret=_s("signSecret"))

    if kind == "mattermost":
        base, token, channel = _s("baseUrl"), _s("botToken"), _s("channelId")
        if not base or not token or not channel:
            log.info("mattermost channel enabled but baseUrl/botToken/channelId is missing")
            return None
        from .providers.mattermost import MattermostProvider  # noqa: PLC0415

        return MattermostProvider(base_url=base, bot_token=token, channel_id=channel)

    return None


__all__ = [
    "KINDS",
    "OUTBOUND_KINDS",
    "POLL_KINDS",
    "PROVIDERS",
    "PUSH_KINDS",
    "build_provider",
    "describe",
    "is_configured",
    "spec_for",
]
