import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const root = fileURLToPath(new URL("../src/", import.meta.url));
const forbidden = [
  /password/i,
  /secret/i,
  /credential/i,
  new RegExp("dangerously" + "SetInnerHTML", "i"),
  /PADOS_DB_/i,
  /postgresql:\/\//i,
  /connection_string/i,
];
// Exact HTML attribute tokens for masked sign-in inputs are not secrets; they
// are stripped before matching so the rules above stay strict everywhere else.
// WebAuthn standard API names and public key-handle ids are likewise not secrets: the private
// key never leaves the authenticator and the browser only relays signed, single-use assertions.
const allowedTokens = [
  'type="password"', 'autoComplete="current-password"',
  "navigator.credentials", "PublicKeyCredential", "allowCredentials", "pubKeyCredParams", "credential_id",
];
const findings = [];

function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? files(path) : /\.(ts|tsx)$/.test(entry.name) ? [path] : [];
  });
}

for (const file of files(root)) {
  const source = allowedTokens.reduce((text, token) => text.split(token).join(""), readFileSync(file, "utf8"));
  for (const pattern of forbidden) {
    if (pattern.test(source)) findings.push(`${file}: ${pattern}`);
  }
}

if (findings.length) {
  console.error(findings.join("\n"));
  process.exit(1);
}
console.log("PADOS web boundary lint passed");
