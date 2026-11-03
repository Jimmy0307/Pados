// SPDX-License-Identifier: AGPL-3.0-only
// OP-007 / P2: duplicate-write protection is per client. Two clients (two sign-ins or two sessions) that send the same
// write at the same time each get their own request and their own answer, never the other's challenge or response.
import { describe, expect, it } from "vitest";
import { RemoteClient } from "./remote-api";

type Call = (path: string, init?: RequestInit) => Promise<{ challenge_id: string }>;

function heldFetcher() {
  const seen: { actor: string; proof: string }[] = [];
  const release: (() => void)[] = [];
  const fetcher = (_input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    const actor = headers.get("X-PADOS-Remote-Actor") || "";
    seen.push({ actor, proof: headers.get("X-PADOS-Remote-Proof") || "" });
    // Held open until both clients have sent, so the two writes are genuinely in flight together.
    return new Promise<Response>((resolve) => release.push(() =>
      resolve(new Response(JSON.stringify({ challenge_id: `challenge-for-${actor}` }), { status: 200 }))));
  };
  return { fetcher: fetcher as typeof fetch, seen, release };
}

const ticks = async (n = 20) => { for (let i = 0; i < n; i += 1) await new Promise((r) => setTimeout(r, 0)); };

const call = (client: RemoteClient): Call => (client as unknown as { call: Call }).call.bind(client);
const stepUp = { method: "POST", body: JSON.stringify({ action: "APPROVE" }) };

describe("remote single flight", () => {
  it("never shares an in-flight write, or its response, between two clients", async () => {
    const shared = heldFetcher();
    const alice = new RemoteClient({ actor: "alice", proof: "proof-a" }, shared.fetcher);
    const bob = new RemoteClient({ actor: "bob", proof: "proof-b" }, shared.fetcher);
    const first = call(alice)("/approvals/appr-1/step-up", stepUp);
    const second = call(bob)("/approvals/appr-1/step-up", stepUp);
    await ticks();
    expect(shared.seen.map((s) => s.actor)).toEqual(["alice", "bob"]);  // two sends, each with its own actor and proof
    shared.release.forEach((go) => go());
    expect((await first).challenge_id).toBe("challenge-for-alice");
    expect((await second).challenge_id).toBe("challenge-for-bob");
  });

  it("still merges a double click within one client", async () => {
    const shared = heldFetcher();
    const alice = new RemoteClient({ actor: "alice", proof: "proof-a" }, shared.fetcher);
    const first = call(alice)("/approvals/appr-1/step-up", stepUp);
    const second = call(alice)("/approvals/appr-1/step-up", stepUp);
    await ticks();
    expect(shared.seen).toHaveLength(1);
    shared.release.forEach((go) => go());
    expect(await first).toEqual(await second);
  });
});
