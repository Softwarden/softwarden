// The session gate (src/lib/session-gate.ts): a session change waits for the requests in flight, holds the requests asked
// for meanwhile until it is done, and gives up waiting after its timeout. Run by `npm run lint` (Node strips the
// TypeScript types; the module has no imports).
import assert from "node:assert/strict";
import test from "node:test";
import { createSessionGate } from "../src/lib/session-gate.ts";

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const tick = () => new Promise((done) => setTimeout(done, 5));

test("a change waits for the request in flight, and a request asked for meanwhile waits for the change", async () => {
  const gate = createSessionGate(10_000);
  const order = [];
  const read = deferred();
  const first = gate.tracked(false, async () => { await read.promise; order.push("read answered"); });
  assert.equal(gate.pending(), 1);
  const change = gate.change(() => gate.tracked(true, async () => { order.push("change sent"); }));
  await tick();
  const later = gate.tracked(false, async () => { order.push("later read sent"); });
  await tick();
  assert.deepEqual(order, []); // the change waits for the read; the later read waits for the change
  read.resolve();
  await Promise.all([first, change, later]);
  assert.deepEqual(order, ["read answered", "change sent", "later read sent"]);
  assert.equal(gate.pending(), 0);
});

test("a request that never answers holds a change only until the timeout, and the queue is released after it", async () => {
  const gate = createSessionGate(40);
  const stuck = gate.tracked(false, () => new Promise(() => {})); // never settles
  void stuck;
  const started = Date.now();
  let sent = false;
  await gate.change(() => gate.tracked(true, async () => { sent = true; }));
  assert.ok(sent);
  assert.ok(Date.now() - started >= 35, "waited about the timeout");
  // Released: a later request runs at once, and another change doesn't wait on the first one.
  let later = false;
  await gate.tracked(false, async () => { later = true; });
  assert.ok(later);
});

test("a change whose request fails still releases the queue", async () => {
  const gate = createSessionGate(1000);
  await assert.rejects(gate.change(() => gate.tracked(true, async () => { throw new Error("refused"); })), /refused/);
  let ran = false;
  await gate.tracked(false, async () => { ran = true; });
  assert.ok(ran);
});
