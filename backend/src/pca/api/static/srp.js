// Telegram 2FA SRP-2048 proof, computed in the browser so the password never leaves the device.
// Mirrors telethon.password.compute_check; parameters were validated server-side.

const SIZE = 256;

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

function bytesToHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

function toBig(bytes) {
  return bytes.length ? BigInt("0x" + bytesToHex(bytes)) : 0n;
}

function toBytes(n) {
  const hex = n.toString(16).padStart(SIZE * 2, "0");
  if (hex.length > SIZE * 2) throw new Error("number too large");
  return hexToBytes(hex);
}

function modPow(base, exp, mod) {
  let result = 1n;
  base %= mod;
  while (exp > 0n) {
    if (exp & 1n) result = (result * base) % mod;
    base = (base * base) % mod;
    exp >>= 1n;
  }
  return result;
}

async function sha256(...parts) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", concat(...parts)));
}

async function pbkdf2(password, salt) {
  const key = await crypto.subtle.importKey("raw", password, "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-512", salt, iterations: 100000 }, key, 512);
  return new Uint8Array(bits);
}

const sh = (data, salt) => sha256(salt, data, salt);

async function passwordHash(password, salt1, salt2) {
  const ph1 = await sh(await sh(password, salt1), salt2);
  return sh(await pbkdf2(ph1, salt1), salt2);
}

function randomBytes(n) {
  const out = new Uint8Array(n);
  crypto.getRandomValues(out);
  return out;
}

export async function computeSrpCheck(params, password, rand = randomBytes) {
  const pBytes = hexToBytes(params.p);
  const salt1 = hexToBytes(params.salt1);
  const salt2 = hexToBytes(params.salt2);
  const gbBytes = hexToBytes(params.srp_b);
  const p = toBig(pBytes);
  const g = BigInt(params.g);
  const gB = toBig(gbBytes);
  if (gB <= 1n || gB >= p - 1n) throw new Error("invalid server value");

  const gPadded = toBytes(g);
  const gbPadded = toBytes(gB);
  const x = toBig(await passwordHash(new TextEncoder().encode(password), salt1, salt2));
  const k = toBig(await sha256(pBytes, gPadded));
  const kv = (k * modPow(g, x, p)) % p;

  for (;;) {
    const a = toBig(rand(SIZE));
    const gA = modPow(g, a, p);
    if (gA <= 1n || gA >= p - 1n) continue;
    const gaPadded = toBytes(gA);
    const u = toBig(await sha256(gaPadded, gbPadded));
    if (u === 0n) continue;
    let t = (gB - kv) % p;
    if (t < 0n) t += p;
    const sA = modPow(t, a + u * x, p);
    const kA = await sha256(toBytes(sA));
    const hp = await sha256(pBytes);
    const hg = await sha256(gPadded);
    const xored = hp.map((b, i) => b ^ hg[i]);
    const m1 = await sha256(xored, await sha256(salt1), await sha256(salt2), gaPadded, gbPadded, kA);
    return { srp_id: params.srp_id, a: bytesToHex(gaPadded), m1: bytesToHex(m1) };
  }
}
