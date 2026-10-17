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


SUPPORT_PROMPT = (
    "<b>✉️ Message support</b>\n\n"
    "Send your message as a single text (up to 2000 characters). Include a payment reference or audit number if it's about one.\n\n"
    "Never include login codes, 2FA passwords or session data."
)
SUPPORT_CANCELLED = "Support message cancelled."
SUPPORT_TOO_LONG = "That message is over 2000 characters. Please shorten it and send it again."
SUPPORT_TOO_MANY = "You already have 3 open tickets. We'll reply to those first; you'll get a message here when we do."


def support_received_text(ticket_id: int) -> str:
    return f"✅ Ticket <b>#{ticket_id}</b> received. We'll reply here in this chat."


def support_admin_alert(ticket_id: int, who: str, message: str) -> str:
    preview = message if len(message) <= 800 else message[:800] + "…"
    return (
        f"<b>New support ticket #{ticket_id}</b> from {esc(who)}\n\n{esc(preview)}\n\n"
        f"Reply: <code>/reply {ticket_id} your message</code>"
    )


def support_reply_text(ticket_id: int, reply: str) -> str:
    return f"<b>🛟 Support reply · ticket #{ticket_id}</b>\n\n{esc(reply)}"


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


AUDIT_ERRORS = {
    "nothing_to_audit": "This channel has no pending join requests to audit right now.",
    "too_many_active": "You already have the maximum number of audits running. Wait for one to finish.",
    "already_running": "An audit of this channel is already running.",
    "insufficient_credits": "You don't have enough credits for this audit. Buy credits, then try again.",
    "account_disconnected": "Your Telegram account is no longer connected. Connect it again first.",
    "channel_unavailable": "Your connected account can no longer audit this channel.",
}


def audit_error(code: str) -> str:
    return "⚠️ " + AUDIT_ERRORS.get(code, "The audit couldn't be started. Please try again shortly.")


def audit_quote_text(channel, quote, available: int) -> str:
    cap = (
        f"\n⚠️ Only the first {quote.scan_target:,} of {quote.pending_requests:,} requests will be scanned "
        "(the per-audit limit)."
        if quote.capped
        else ""
    )
    afford = (
        "Confirm to start. Credits are reserved now and only charged if the audit completes."
        if available >= quote.credits
        else f"You need {quote.credits - available} more credit(s)."
    )
    return (
        f"<b>🔍 Audit {esc(channel.title)}</b>\n\n"
        f"Pending join requests: {quote.pending_requests:,}\n"
        f"Price: <b>{quote.credits} credit(s)</b> · Your balance: {available}{cap}\n\n"
        "The audit is read-only: nobody is approved, rejected or contacted.\n"
        f"{afford}"
    )


def audit_started_text(job, channel_title: str) -> str:
    return (
        f"<b>✅ Audit #{job.id} queued</b> for {esc(channel_title)}\n\n"
        "We'll post progress here and message you when the report is ready. "
        "You can cancel at any time before it finishes; cancelled audits are not charged."
    )


def audit_progress_text(view) -> str:
    total = view.expected
    line = (
        f"{view.scanned:,} of about {total:,} requests scanned ({min(100, round(view.scanned * 100 / total))}%)"
        if total
        else f"{view.scanned:,} requests scanned"
    )
    return f"<b>⏳ Audit #{view.job_id} · {esc(view.channel_title)}</b>\n\nStatus: {esc(view.status_label)}\n{line}"


AUDIT_NOT_FOUND = "That audit doesn't exist or isn't yours."
AUDIT_CANCEL_CONFIRM = (
    "<b>Cancel this audit?</b>\n\nScanning stops, collected data is deleted and your reserved credits are released."
)
REPORT_USAGE = "Usage: /report 123 (the audit number from My Audits)."
REPORT_UNAVAILABLE = "A report is only available for completed or partial audits."
CSV_UNAVAILABLE = "The CSV export is no longer available. Raw rows are deleted after the retention period."
CSV_CAPTION = (
    "Minimised export: one row per scanned request, without names, usernames, ids or exact times."
)
COMPARE_PICK_A = "<b>⚖️ Compare Channels</b>\n\nStep 1 of 2: choose the first completed audit (A)."
COMPARE_BASE_NOT_COMPLETED = "Only completed audits can be compared."


def compare_pick_b(title: str, job_id: int) -> str:
    return f"<b>⚖️ Compare Channels</b>\n\nA: {esc(title)} (#{job_id})\nStep 2 of 2: choose the audit to compare it with (B)."


def compare_error(code: str) -> str:
    if code == "same_audit":
        return "Choose two different audits to compare."
    return "Both audits must be completed to compare them. Partial audits aren't comparable."


def audit_history_text(count: int) -> str:
    return f"<b>📊 My Audits</b>\n\n{count} audit(s). Tap one to open its status or report."


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


def _money(currency: str, minor: int) -> str:
    symbol = "₦" if currency.upper() == "NGN" else f"{currency} "
    return f"{symbol}{minor / 100:,.2f}"


def package_label(package) -> str:
    return f"{package.name} · {package.credits} credits · {_money(package.currency, package.price_minor)}"


def buy_packages_text(packages) -> str:
    lines = ["<b>💳 Buy Credits</b>\n", "Choose a package:"]
    for package in packages:
        lines.append(f"• {esc(package_label(package))}")
    lines.append("\nPayments are processed securely by Paystack. Credits never expire.")
    return "\n".join(lines)


def checkout_text(payment) -> str:
    return (
        "<b>💳 Checkout ready</b>\n\n"
        f"Package: <b>{payment.credits} credits</b>\n"
        f"Amount: <b>{_money(payment.currency, payment.amount)}</b>\n"
        f"Reference: <code>{esc(payment.reference)}</code>\n\n"
        "Tap the button below to pay on Paystack. Credits are added automatically once Paystack confirms "
        "the payment. If they don't appear within a minute, tap “check status”."
    )


def payment_pending_text(payment) -> str:
    return (
        "<b>⏳ Waiting for confirmation</b>\n\n"
        f"We haven't received confirmation from Paystack for <code>{esc(payment.reference)}</code> yet.\n"
        "If you've just paid, wait a moment and check again. Unpaid checkouts expire automatically."
    )


def payment_status_text(payment, available: int) -> str:
    if payment.status == "success":
        return (
            "<b>✅ Payment confirmed</b>\n\n"
            f"<b>{payment.credits}</b> credits were added to your wallet.\n"
            f"Available balance: <b>{available}</b> credits."
        )
    if payment.status == "abandoned":
        return "<b>Checkout expired</b>\n\nThis checkout wasn't completed. You can start a new one from Buy Credits."
    return (
        "<b>❌ Payment not completed</b>\n\n"
        "Paystack reported this payment as unsuccessful, so no credits were added. "
        f"If you were charged, contact support (/support) with reference <code>{esc(payment.reference)}</code>."
    )


_PAYMENT_ERRORS = {
    "package_unavailable": "That package is no longer available. Please choose another.",
    "too_many_open_checkouts": "You have several unfinished checkouts. Complete one or wait for them to expire.",
    "provider_unreachable": "We couldn't reach Paystack. Please try again in a moment.",
    "provider_rejected": "Paystack couldn't start this checkout. Please try again later.",
    "provider_bad_response": "Paystack returned an unexpected response. Please try again later.",
    "not_found": "We couldn't find that payment.",
    "not_configured": "Payments aren't available right now.",
}


def payment_error(code: str) -> str:
    return "<b>💳 Buy Credits</b>\n\n" + esc(_PAYMENT_ERRORS.get(code, "Something went wrong. Please try again."))


_PAYMENT_STATUS_LABEL = {
    "initialized": "⏳ pending",
    "success": "✅ paid",
    "failed": "❌ failed",
    "abandoned": "⌛ expired",
}


def payment_history_text(history) -> str:
    if not history:
        return "<b>🧾 Payment history</b>\n\nNo payments yet."
    lines = ["<b>🧾 Payment history</b>\n"]
    for payment in history:
        label = _PAYMENT_STATUS_LABEL.get(payment.status, payment.status)
        lines.append(
            f"• {payment.created_at:%Y-%m-%d} · {payment.credits} credits · "
            f"{_money(payment.currency, payment.amount)} · {label}"
        )
    return "\n".join(lines)


def admin_stats_text(stats: PlatformStats) -> str:
    revenue = ", ".join(f"{cur} {amount / 100:,.2f}" for cur, amount in stats.revenue_by_currency.items()) or "none"
    return (
        "<b>🛠 Admin Overview</b>\n\n"
        f"Customers: <b>{stats.total_customers}</b> (active 30d: {stats.active_30d}, suspended: {stats.suspended})\n"
        f"Connected accounts: {stats.connected_accounts}\n"
        f"Audits: completed {stats.audits_completed} · partial {stats.audits_partial} · "
        f"failed {stats.audits_failed} · cancelled {stats.audits_cancelled} · active {stats.audits_active}\n"
        f"Verified revenue: {revenue}\n"
        f"Payments: {stats.payments_success} paid · {stats.payments_pending} pending · {stats.payments_failed} failed/abandoned\n"
        f"Credits: {stats.credits_sold} sold · {stats.credits_promotional} granted/promo · {stats.credits_consumed} consumed\n"
        f"Credit liabilities: {stats.credits_available} available + {stats.credits_reserved} reserved\n"
        f"Open support tickets: {stats.open_tickets} · Security events (24h): {stats.security_events_24h}\n\n"
        "<b>Monitoring</b>\n"
        "/health · /jobs · /failures · /security · /admin_log\n\n"
        "<b>Customers</b>\n"
        "/user &lt;telegram_id&gt;\n"
        "/suspend &lt;telegram_id&gt; · /unsuspend &lt;telegram_id&gt;\n"
        "/grant &lt;telegram_id&gt; &lt;credits&gt; &lt;reason&gt;\n"
        "/promo &lt;telegram_id&gt; &lt;credits&gt; &lt;reason&gt;\n"
        "/debit &lt;telegram_id&gt; &lt;credits&gt; &lt;reason&gt;\n\n"
        "<b>Audits</b>\n"
        "/job_cancel &lt;audit_id&gt; · /job_retry &lt;audit_id&gt;\n"
        "/audit_refund &lt;audit_id&gt; &lt;reason&gt; (refund an incomplete, charged audit)\n\n"
        "<b>Pricing and packages</b>\n"
        "/admin_pricing · /tier_set &lt;min&gt; &lt;max&gt; &lt;credits&gt; · /tier_off &lt;tier_id&gt;\n"
        "/packages · /package_set &lt;id|new&gt; &lt;credits&gt; &lt;price_naira&gt; &lt;name&gt; · /package_off &lt;id&gt;\n\n"
        "<b>Support</b>\n"
        "/tickets · /reply &lt;ticket_id&gt; &lt;message&gt; · /close_ticket &lt;ticket_id&gt;\n\n"
        "<b>Subscription</b>\n"
        "/sub_channel &lt;chat_id&gt; · /sub_enforce on|off · /sub_status"
    )
