// Session changes (starting and stopping an impersonation) rotate the session key. A request still carrying the old key
// that is answered after the change finds no session, and the answer can clear the session cookie, signing the browser
// out (seen when a read raced Stop). So a change waits until this tab's requests in flight have been answered (at most
// `timeoutMs`, so a request that never answers can't block it), and requests asked for meanwhile wait until the change
// has been answered (and its new cookie stored). Used by src/api.ts; no imports, so `npm run lint` tests it with Node
// (scripts/session-gate.test.mjs).

export type SessionGate = {
  /** Run one request: it waits while a change runs (unless it is the change's own request, `exclusive`), and counts as in
   *  flight until it settles. */
  tracked<T>(exclusive: boolean, run: () => Promise<T>): Promise<T>;
  /** Run `run` (whose request is sent with `exclusive`) alone: after the requests in flight, before any asked for later. */
  change<T>(run: () => Promise<T>): Promise<T>;
  /** Requests in flight now (for tests). */
  pending(): number;
};

export function createSessionGate(timeoutMs = 5000): SessionGate {
  let inFlight = 0;
  const drained: (() => void)[] = [];
  let gate: Promise<void> | null = null;

  async function tracked<T>(exclusive: boolean, run: () => Promise<T>): Promise<T> {
    if (!exclusive) while (gate) await gate;
    inFlight++;
    try {
      return await run();
    } finally {
      inFlight--;
      if (inFlight === 0) drained.splice(0).forEach((done) => done());
    }
  }

  async function change<T>(run: () => Promise<T>): Promise<T> {
    while (gate) await gate;
    let open!: () => void;
    gate = new Promise<void>((done) => { open = done; });
    try {
      if (inFlight > 0) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        await Promise.race([
          new Promise<void>((done) => drained.push(done)),
          new Promise<void>((done) => { timer = setTimeout(done, timeoutMs); }),
        ]);
        clearTimeout(timer);
      }
      return await run();
    } finally {
      gate = null;
      open();
    }
  }

  return { tracked, change, pending: () => inFlight };
}
