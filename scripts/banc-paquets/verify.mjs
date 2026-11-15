// Vérificateur de référence du banc : implémentation indépendante de l'ADR-0006
// avec WebCrypto, c'est-à-dire ce que fera le plugin. Usage :
//   node verify.mjs <paquet> <enveloppe.json> <dm-trust.json> <version> <slug>
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
const subtle = webcrypto.subtle;
const b64 = (s) => Uint8Array.from(Buffer.from(s, "base64"));
const hex = (buf) => Buffer.from(buf).toString("hex");

async function importKey(spkiB64) {
  return subtle.importKey("spki", b64(spkiB64), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
}
async function signedPayload(part, keys) {
  const payload = b64(part.payload);
  for (const s of part.signatures || []) {
    if (s.alg !== "ES256" || !keys[s.keyid]) continue;
    const ok = await subtle.verify({ name: "ECDSA", hash: "SHA-256" }, keys[s.keyid], b64(s.sig), payload);
    if (ok) return payload;
    throw new Error("bad_signature");
  }
  throw new Error("unknown_key");
}
async function verify(pkg, envelope, trust, version, slug, now = new Date()) {
  if (!envelope || envelope.schema !== "dm-package-signature/1") return "malformed";
  const keys = {};
  for (const r of trust.roots || []) keys[r.keyid] = await importKey(r.public_key);
  const roots = { ...keys };
  if (envelope.delegation) {
    let d;
    try { d = JSON.parse(Buffer.from(await signedPayload(envelope.delegation, roots)).toString("utf8")); }
    catch (e) { return "delegation_invalid:" + e.message; }
    if (d.schema !== "dm-signing-delegation/1") return "delegation_invalid:schema";
    if (!(new Date(d.not_before) <= now && now <= new Date(d.not_after))) return "delegation_invalid:dates";
    const spki = b64(d.public_key);
    if (hex(await subtle.digest("SHA-256", spki)).slice(0, 16) !== d.keyid) return "delegation_invalid:keyid";
    keys[d.keyid] = await importKey(d.public_key);
  }
  let st;
  try { st = JSON.parse(Buffer.from(await signedPayload(envelope, keys)).toString("utf8")); }
  catch (e) { return e.message; }
  if (st.schema !== "dm-package-statement/1" || st.kind !== "served") return "wrong_kind";
  if (st.sha256 !== hex(await subtle.digest("SHA-256", pkg))) return "digest_mismatch";
  if (st.version !== version) return "version_mismatch";
  if (st.slug !== slug) return "slug_mismatch";
  return "ok";
}
const [pkgPath, envPath, trustPath, version, slug, nowIso] = process.argv.slice(2);
const res = await verify(readFileSync(pkgPath), JSON.parse(readFileSync(envPath, "utf8")),
  JSON.parse(readFileSync(trustPath, "utf8")), version, slug, nowIso ? new Date(nowIso) : new Date());
console.log(res);
