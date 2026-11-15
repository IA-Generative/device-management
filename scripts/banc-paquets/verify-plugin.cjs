// Le module de vérification du plugin Thunderbird lui-même (dépôt iassistant-matisse,
// thunderbird/60.9.1/modules/package-signature.js), chargé tel quel depuis PLUGIN_VERIFIER. Usage : node verify-plugin.cjs <paquet> <enveloppe> <trust> <version> <extensionId> <slug> [nowIso]
const { readFileSync } = require("node:fs");
const { webcrypto } = require("node:crypto");
const { verifyPackageSignature } = require(process.env.PLUGIN_VERIFIER || "./plugin-package-signature.js");
const [pkg, env, trust, version, extensionId, slug, nowIso] = process.argv.slice(2);
verifyPackageSignature({
  subtle: webcrypto.subtle,
  envelope: JSON.parse(readFileSync(env, "utf8")),
  bytes: new Uint8Array(readFileSync(pkg)),
  version, extensionId: extensionId || undefined, slug,
  trust: JSON.parse(readFileSync(trust, "utf8")),
  now: nowIso ? Date.parse(nowIso) : undefined,
}).then((r) => console.log(r.reason));
