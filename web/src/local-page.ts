// SPDX-License-Identifier: AGPL-3.0-only
// The loopback check shared by the control token path and the browser identity client: both only ever talk to a page
// this computer serves to itself, never to a build of this page hosted elsewhere (a static site).
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

/** The control token and the identity calls are only ever sent from a page this computer serves to itself. */
export function isLocalPage(hostname: string | undefined = globalThis.location?.hostname): boolean {
  return LOOPBACK_HOSTS.has(String(hostname || "").toLowerCase());
}
