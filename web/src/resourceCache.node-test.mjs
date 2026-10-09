import test from "node:test";
import assert from "node:assert/strict";
import { loadResourceOnce } from "./resourceCache.ts";

test("concurrent reads deduplicate; resolved values are not cached", async () => {
  let calls = 0;
  let complete;
  const loader = () => { calls++; return new Promise(resolve => { complete = resolve; }); };
  const a = loadResourceOnce("project-a", loader);
  const b = loadResourceOnce("project-a", loader);
  assert.equal(a, b);
  assert.equal(calls, 1);
  complete("first");
  assert.equal(await a, "first");
  assert.equal(await loadResourceOnce("project-a", async () => "refreshed"), "refreshed");
});

test("separate project keys never share data; failures allow retry", async () => {
  const results = await Promise.all([
    loadResourceOnce("a", async () => "A"), loadResourceOnce("b", async () => "B"),
  ]);
  assert.deepEqual(results, ["A", "B"]);
  await assert.rejects(loadResourceOnce("a", async () => { throw Error("unavailable"); }));
  assert.equal(await loadResourceOnce("a", async () => "recovered"), "recovered");
});
