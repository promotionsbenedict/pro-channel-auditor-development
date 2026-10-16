import { computeSrpCheck } from "/connect/static/srp.js";

const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const token = new URLSearchParams(location.search).get("t") || "";
history.replaceState(null, "", "/connect");

const $ = (id) => document.getElementById(id);
const SCREENS = ["loading", "intro", "phone", "code", "password", "done", "ended"];

const MESSAGES = {
  unauthorized: "Please open this page from the bot's Connect button inside Telegram.",
  link_invalid: "This link isn't valid. Request a new one from the bot.",
  link_expired: "This link has expired. Request a new one from the bot.",
  link_used: "This link has already been used. Request a new one from the bot.",
  wrong_user: "This link was issued to a different Telegram user.",
  account_unavailable: "Your account can't connect right now. Contact support from the bot.",
  account_mismatch: "That phone number belongs to a different Telegram account. Connect the account you're using right now.",
  phone_invalid: "That phone number doesn't look right. Include the country code.",
  phone_banned: "Telegram has banned this phone number.",
  not_registered: "No Telegram account uses this phone number.",
  code_invalid: "That code is incorrect. Check it and try again.",
  code_expired: "The code expired. Enter your phone number again to get a new one.",
  password_invalid: "Incorrect password. Try again.",
  password_challenge_expired: "The password check timed out. Try again.",
  password_flood: "Too many password attempts. Telegram has paused logins for this account; try later.",
  too_many_attempts: "Too many incorrect attempts. Request a new link from the bot.",
  too_many_codes: "Too many codes requested. Try again in an hour.",
  resend_unavailable: "Another code can't be sent yet. Use the code you already received.",
  unsupported_delivery: "Telegram chose a code method we can't use on the web. Try again later.",
  unsupported_password: "Your account's password type isn't supported.",
  email_setup_required: "Telegram requires you to set up a login email in the Telegram app first.",
  payment_required: "Telegram requires a paid verification for this number. Log in using the Telegram app instead.",
  flood: "Telegram asked us to slow down.",
  rate_limited: "Too many requests from your network. Try again later.",
  telegram_unavailable: "Telegram didn't respond. Please try again.",
  connect_disabled: "Account connection is temporarily unavailable.",
  bad_origin: "Please open this page from the bot.",
  wrong_step: "This step is out of date. Reloading…",
};
const TERMINAL = new Set([
  "link_invalid", "link_expired", "link_used", "wrong_user", "account_unavailable", "account_mismatch",
  "too_many_attempts", "phone_banned", "not_registered", "unsupported_delivery", "unsupported_password",
  "email_setup_required", "payment_required", "password_flood", "connect_disabled", "unauthorized",
]);
const DELIVERY = {
  app: "We sent the code to your Telegram app. Check the Telegram service chat on another device.",
  sms: "We sent the code by SMS",
  call: "You'll receive a phone call with the code",
  missed_call: "You'll receive a missed call; the code is the last digits of the calling number",
  email: "We sent the code to your login email",
  fragment: "We sent the code to your anonymous number on Fragment",
};

let resendTimer = null;

function show(name) {
  for (const s of SCREENS) $(`screen-${s}`).hidden = s !== name;
  $("cancel").hidden = !["phone", "code", "password"].includes(name);
  const first = $(`screen-${name}`).querySelector("input:not([type=checkbox])");
  if (first) setTimeout(() => first.focus(), 50);
}

function alertMsg(text) {
  $("alert").textContent = text || "";
  $("alert").hidden = !text;
}

function describe(error, retryAfter) {
  let text = MESSAGES[error] || "Something went wrong. Please try again.";
  if (error === "flood" && retryAfter) text += ` Try again in ${Math.ceil(retryAfter / 60)} min.`;
  return text;
}

function end(text) {
  alertMsg("");
  $("ended-text").textContent = text;
  show("ended");
}

async function api(path, body = {}) {
  const res = await fetch(`/api/connect/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": initData },
    body: JSON.stringify({ token, ...body }),
    credentials: "omit",
    cache: "no-store",
  });
  let data;
  try {
    data = await res.json();
  } catch {
    data = { ok: false, error: "internal" };
  }
  return data;
}

async function busy(button, fn) {
  if (button.getAttribute("aria-busy") === "true") return;
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  alertMsg("");
  try {
    await fn();
  } catch {
    alertMsg(MESSAGES.telegram_unavailable);
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

function handleError(data) {
  if (TERMINAL.has(data.error)) return end(describe(data.error));
  if (data.error === "wrong_step") return load();
  if (data.error === "code_expired") {
    alertMsg(describe(data.error));
    return show("phone");
  }
  alertMsg(describe(data.error, data.retry_after));
}

function startResendCountdown(seconds) {
  clearInterval(resendTimer);
  const btn = $("resend");
  let left = Math.max(0, Math.floor(seconds || 0));
  const tick = () => {
    btn.disabled = left > 0;
    btn.textContent = left > 0 ? `Resend code in ${left}s` : "Resend code";
    if (left-- <= 0) clearInterval(resendTimer);
  };
  tick();
  resendTimer = setInterval(tick, 1000);
}

function showCode(data) {
  const d = data.delivery || {};
  const where = DELIVERY[d.kind] || "We sent you a login code";
  $("code-where").textContent = d.kind === "app" ? where : `${where} to ${data.phone_masked || "your number"}.`;
  if (d.length) $("code").maxLength = d.length;
  $("code").value = "";
  $("resend").hidden = !d.next_kind;
  startResendCountdown(d.timeout || 30);
  show("code");
}

async function showPassword() {
  const data = await api("password/params");
  if (!data.ok) return handleError(data);
  const hint = data.password?.hint;
  $("password-hint").textContent = hint ? `Hint: ${hint}` : "";
  $("password-hint").hidden = !hint;
  show("password");
}

function done(data) {
  $("done-name").textContent = data.account?.display_name ? `as ${data.account.display_name}` : "";
  tg?.HapticFeedback?.notificationOccurred("success");
  show("done");
}

async function route(data) {
  switch (data.status) {
    case "issued": return show("intro");
    case "code_sent": return showCode(data);
    case "password_required": return showPassword();
    case "completed": return data.account ? done(data) : end("This account is already connected. You can close this page.");
    case "cancelled": return end("This connection was cancelled. Request a new link from the bot.");
    case "expired": return end(MESSAGES.link_expired);
    default: return end(MESSAGES.link_used);
  }
}

async function load() {
  show("loading");
  if (!initData) return end(MESSAGES.unauthorized);
  if (!token) return end(MESSAGES.link_invalid);
  const data = await api("status");
  if (!data.ok) return handleError(data);
  return route(data);
}

$("consent").addEventListener("change", (e) => { $("intro-next").disabled = !e.target.checked; });
$("intro-next").addEventListener("click", () => show("phone"));

$("phone-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const button = e.submitter || e.target.querySelector("button");
  busy(button, async () => {
    const data = await api("phone", { phone: $("phone").value });
    data.ok ? showCode(data) : handleError(data);
  });
});

$("code-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const button = e.submitter || e.target.querySelector("button");
  busy(button, async () => {
    const data = await api("code", { code: $("code").value.replace(/\s/g, "") });
    if (!data.ok) return handleError(data);
    if (data.status === "password_required") return showPassword();
    return route(data);
  });
});

$("resend").addEventListener("click", (e) => busy(e.currentTarget, async () => {
  const data = await api("resend");
  data.ok ? showCode(data) : handleError(data);
}));

$("password-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const button = e.submitter || e.target.querySelector("button");
  busy(button, async () => {
    const params = await api("password/params");
    if (!params.ok) return handleError(params);
    const proof = await computeSrpCheck(params.password, $("password").value);
    const data = await api("password", proof);
    if (!data.ok) {
      $("password").value = "";
      return handleError(data);
    }
    $("password").value = "";
    return route(data);
  });
});

$("cancel").addEventListener("click", async () => {
  await api("cancel");
  end("Connection cancelled. Nothing was saved.");
});

const close = () => (tg?.close ? tg.close() : window.close());
$("close").addEventListener("click", close);
$("ended-close").addEventListener("click", close);

tg?.ready();
tg?.expand();
load();
