"""All customer-facing copy. Customer-controlled values must pass through esc()."""

from __future__ import annotations

from html import escape

from pca.db.models import CreditTransaction
from pca.services.admin import PlatformStats
from pca.services.audits import AuditListItem


def esc(value: object) -> str:
    return escape(str(value), quote=False)


DEFAULT_GATE_TEXT = (
    "<b>Welcome to Pro Channel Auditor! 🔍</b>\n\n"
    "Analyze Telegram channel audiences, examine pending requests, and make smarter "
    "channel-buying decisions.\n\n"
    "To continue, please join our official updates channel."
)

WELCOME = (
    "<b>Pro Channel Auditor 🔍</b>\n\n"
    "Read-only audits of pending join requests for channels you administer.\n"
    "Choose an option below."
)

SUBSCRIPTION_UNAVAILABLE = (
    "⚠️ <b>We couldn't verify your channel membership right now.</b>\n\n"
    "This is a temporary problem on our side or Telegram's, not your account. "
    "Please try again in a moment."
)

NOT_YET_JOINED_ALERT = (
    "We couldn't find your membership yet. Join the channel, then tap Verify again."
)

SUSPENDED = (
    "🚫 <b>Your account is suspended.</b>\n\n"
    "Your credits and reports are retained. Use /support to contact us."
)

DELETION_PENDING = (
    "🗑 <b>Your deletion request is being processed.</b>\n\n"
    "Feature access is paused. Use /support if you made this request by mistake."
)

GENERIC_ERROR = "Something went wrong while processing that. Please try again shortly."

ADMIN_DENIED = "This command is not available."


def help_text(support_contact: str) -> str:
    return (
        "<b>📚 Help</b>\n\n"
        "<b>How it works</b>\n"
        "1. Join our updates channel.\n"
        "2. Connect your own Telegram account through our secure HTTPS page.\n"
        "3. Pick a channel you administer that has pending join requests.\n"
        "4. Review the credit quote and confirm.\n"
        "5. Receive a report with activity, profile and timing statistics.\n\n"
        "<b>What we never do</b>\n"
        "We never approve, reject, message or invite requesters, and we never change "
        "channel or account settings. Audits are strictly read-only.\n\n"
        "<b>Commands</b>\n"
        "/start · /audit · /my_audits · /compare · /wallet · /buy\n"
        "/connect · /disconnect · /privacy · /support\n\n"
        f"Support: {esc(support_contact)}"
    )


def support_text(support_contact: str) -> str:
    return (
        "<b>🛟 Support</b>\n\n"
        f"Contact {esc(support_contact)} for help with payments, audits or your account.\n\n"
        "Never share login codes, 2FA passwords, or session data with anyone, "
        "including people claiming to be our staff."
    )


PRIVACY = (
    "<b>🔐 Privacy</b>\n\n"
    "• We store your Telegram user id, username, credit ledger, payments and audit summaries.\n"
    "• A connected Telegram session is encrypted at rest and can be revoked at any time with /disconnect.\n"
    "• Reports contain aggregate statistics only, not requester identities.\n"
    "• Raw requester data is processed transiently and removed after the audit.\n\n"
    "Use the button below to request deletion of your account data. Financial ledger "
    "records are retained where the law requires."
)

DELETE_CONFIRM = (
    "<b>Request account deletion?</b>\n\n"
    "Your connected Telegram session will be revoked immediately and feature access paused. "
    "An administrator will complete deletion of your data."
)

DELETE_DONE = "✅ Deletion requested. Your Telegram session has been revoked."


def disconnect_result(revoked: int, remote_ok: bool = True) -> str:
    if not revoked:
        return "No Telegram account is connected."
    if remote_ok:
        return "✅ Disconnected. The session was logged out of Telegram and wiped from our servers."
    return (
        "✅ Disconnected and wiped from our servers. We couldn't reach Telegram to log the session out, "
        "so also remove it in Telegram under Settings → Devices."
    )


def wallet_text(available: int, reserved: int, transactions: list[CreditTransaction]) -> str:
    lines = [
        "<b>💰 My Wallet</b>\n",
        f"Available credits: <b>{available}</b>",
        f"Reserved for running audits: <b>{reserved}</b>",
    ]
    if transactions:
        lines.append("\n<b>Recent activity</b>")
        for tx in transactions:
            sign = "+" if tx.amount > 0 else ""
            label = tx.transaction_type.replace("_", " ")
            amount = f"{sign}{tx.amount}" if tx.amount else f"{tx.reserved_delta:+} reserved"
            lines.append(f"• {tx.created_at:%Y-%m-%d} · {esc(label)} · {amount}")
    else:
        lines.append("\nNo transactions yet.")
    return "\n".join(lines)


def audits_text(items: list[AuditListItem]) -> str:
    if not items:
        return "<b>📊 My Audits</b>\n\nYou haven't run any audits yet."
    lines = ["<b>📊 My Audits</b>\n"]
    for item in items:
        lines.append(f"#{item.job_id} · {esc(item.channel_title)} · {esc(item.status)} · {item.created_at:%Y-%m-%d}")
    return "\n".join(lines)


def connected_account_text(connection) -> str:
    if connection is None:
        return (
            "<b>👤 Connected Account</b>\n\n"
            "No Telegram account is connected. Use 🔗 Connect Telegram Account to link one."
        )
    since = f"{connection.connected_at:%Y-%m-%d %H:%M} UTC" if connection.connected_at else "unknown"
    lines = ["<b>👤 Connected Account</b>\n"]
    if connection.account_display_name:
        lines.append(f"Name: {esc(connection.account_display_name)}")
    if connection.account_username:
        lines.append(f"Username: @{esc(connection.account_username)}")
    if connection.account_phone_masked:
        lines.append(f"Phone: <code>{esc(connection.account_phone_masked)}</code>")
    lines.append(f"Account id: <code>{connection.connected_account_id}</code>")
    lines.append(f"Connected since: {since}")
    lines.append("\nAccess is read-only. Use /disconnect to log this session out of Telegram and wipe it.")
    return "\n".join(lines)


def connect_intro(minutes: int, *, reconnecting: bool) -> str:
    extra = "\n\nConnecting again replaces your current session." if reconnecting else ""
    return (
        "<b>🔗 Connect Telegram Account</b>\n\n"
        "Tap the button below to open our secure connection page inside Telegram. "
        "You'll confirm your phone number and enter the login code there, never in this chat.\n\n"
        f"The link is personal and expires in {minutes} minutes.{extra}"
    )


SESSION_EXPIRED = (
    "<b>⚠️ Session ended</b>\n\n"
    "Your connected session is no longer valid (it was logged out or revoked in Telegram). "
    "We've wiped it. Connect again to continue."
)

TELEGRAM_UNAVAILABLE = "Telegram didn't respond just now. Please try again in a moment."


def telegram_busy(retry_after: int | None) -> str:
    wait = f" Try again in about {max(1, (retry_after or 60) // 60)} min." if retry_after else ""
    return f"Telegram asked us to slow down.{wait}"


def channels_text(total: int, eligible: int) -> str:
    if total == 0:
        return (
            "<b>📢 My Channels</b>\n\n"
            "Your connected account doesn't administer any channels.\n"
            "Make it an admin of a channel, then tap Refresh."
        )
    return (
        "<b>📢 My Channels</b>\n\n"
        f"Found {total} channel(s) you administer. {eligible} can be audited.\n"
        "✅ can manage join requests · 🚫 missing that permission\n\n"
        "Tap a channel to re-check its permissions."
    )


CHANNEL_GONE = (
    "That channel is no longer available to your connected account. "
    "Open My Channels to refresh the list."
)


def channel_detail_text(channel, auth) -> str:
    username = f" (@{esc(channel.username)})" if channel.username else ""
    role = "Owner" if auth.is_creator else "Administrator"
    pending = auth.pending_requests if auth.pending_requests is not None else "unknown"
    if auth.can_manage_join_requests:
        status = "✅ Can manage join requests. This channel can be audited."
    else:
        status = (
            "🚫 Your account can't manage join requests here. Ask the owner to grant "
            "the “Add Subscribers / Invite Users” admin right, then re-check."
        )
    return (
        f"<b>{esc(channel.title)}</b>{username}\n\n"
        f"Role: {role}\n"
        f"Pending join requests: {pending}\n\n"
        f"{status}"
    )


AUDIT_PICK = "<b>🔍 New Audit</b>\n\nChoose the channel to audit:"

AUDIT_NO_ELIGIBLE = (
    "<b>🔍 New Audit</b>\n\n"
    "None of your channels can be audited yet. Your account needs the right to manage join "
    "requests. Open My Channels to refresh and check permissions."
)


def audit_ready_text(channel, auth) -> str:
    pending = auth.pending_requests if auth.pending_requests is not None else "unknown"
    return (
        f"<b>🔍 {esc(channel.title)}</b>\n\n"
        f"Permissions verified. Pending join requests: {pending}.\n\n"
        "The audit engine is launching soon. No credits have been charged."
    )


CONNECT_NOT_ENABLED = (
    "<b>🔗 Connect Telegram Account</b>\n\n"
    "Secure account connection is being rolled out. It runs on our HTTPS authorization "
    "page with explicit consent; you will never be asked for login codes in this chat.\n\n"
    "We'll announce availability in our channel."
)

AUDIT_REQUIRES_CONNECTION = (
    "<b>🔍 New Audit</b>\n\n"
    "Audits run through your own connected Telegram account. "
    "Connect your account first, then pick a channel you administer."
)

COMPARE_EMPTY = (
    "<b>⚖️ Compare Channels</b>\n\n"
    "You need at least two completed audits to compare channels."
)

CHANNELS_REQUIRES_CONNECTION = (
    "<b>📢 My Channels</b>\n\n"
    "Connect your Telegram account to discover channels you administer."
)

BUY_UNAVAILABLE = (
    "<b>💳 Buy Credits</b>\n\n"
    "Credit packages are not available for purchase yet. Check back soon."
)


def buy_packages_text(packages) -> str:
    lines = ["<b>💳 Buy Credits</b>\n", "Choose a package:"]
    for package in packages:
        lines.append(f"• {esc(package.name)}: {package.credits} credits · {package.currency} {package.price_minor / 100:,.2f}")
    lines.append("\nOnline checkout is being enabled. You'll be able to pay securely via Paystack.")
    return "\n".join(lines)


def admin_stats_text(stats: PlatformStats) -> str:
    revenue = ", ".join(f"{cur} {amount / 100:,.2f}" for cur, amount in stats.revenue_by_currency.items()) or "none"
    return (
        "<b>🛠 Admin Overview</b>\n\n"
        f"Customers: <b>{stats.total_customers}</b> (active 30d: {stats.active_30d}, suspended: {stats.suspended})\n"
        f"Audits: completed {stats.audits_completed} · failed {stats.audits_failed} · active {stats.audits_active}\n"
        f"Verified revenue: {revenue}\n"
        f"Credit liabilities: {stats.credits_available} available + {stats.credits_reserved} reserved\n\n"
        "<b>Commands</b>\n"
        "/admin_pricing — list tiers\n"
        "/tier_set &lt;min&gt; &lt;max&gt; &lt;credits&gt;\n"
        "/tier_off &lt;tier_id&gt;\n"
        "/suspend &lt;telegram_id&gt; · /unsuspend &lt;telegram_id&gt;\n"
        "/grant &lt;telegram_id&gt; &lt;credits&gt; &lt;reason&gt;\n"
        "/sub_channel &lt;chat_id&gt; · /sub_enforce on|off · /sub_status\n"
        "/admin_log — recent admin actions"
    )
