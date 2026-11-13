import { Browser, BrowserContext, expect, Page, test } from "@playwright/test";

/** Shared by the specs: signed-in pages per role and synthetic records made through the API. */
export const PASSWORD = "softwarden-demo-2026"; // synthetic demo accounts from seed_demo

const sessions: Record<string, Promise<Awaited<ReturnType<BrowserContext["storageState"]>>>> = {};

/** The test each open context was first seen in. The browser is the worker's and Playwright closes only the contexts of
 *  its own `context`/`page` fixtures, so the pages a test opens here (or with browser.newContext) stayed open for the
 *  rest of the worker: near the end of a CI shard, with one worker, 86 signed-in pages were open and every action took
 *  seconds, until a test timed out (catalog-fields.spec.ts). pageAs closes the contexts of earlier tests first. A context made
 *  outside pageAs is first seen at the next pageAs call, so at worst it closes one test later. Nothing shares a page
 *  between tests (module state keeps storage states and IDs only). */
const firstSeen = new WeakMap<BrowserContext, string>();

function currentTest(): string | null {
  try {
    const info = test.info();
    return `${info.testId}:${info.retry}:${info.repeatEachIndex}`;
  } catch {
    return null; // not inside a test
  }
}

async function closeEarlierContexts(browser: Browser, now: string) {
  await Promise.all(browser.contexts().map(async (context) => {
    const seen = firstSeen.get(context);
    if (seen === undefined) firstSeen.set(context, now);
    else if (seen !== now) await context.close().catch(() => undefined);
  }));
}

export async function pageAs(browser: Browser, baseURL: string | undefined, email: string): Promise<Page> {
  const now = currentTest();
  if (now) await closeEarlierContexts(browser, now);
  sessions[email] ??= (async () => {
    const page = await browser.newPage({ baseURL });
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    // Every role lands on the Dashboard. A sign-in hashes the password and the Dashboard
    // loads its panels: on a busy machine running the whole suite that took longer than the default 5 s.
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible({ timeout: 15_000 });
    const state = await page.context().storageState();
    await page.close();
    return state;
  })();
  const context = await browser.newContext({ baseURL, storageState: await sessions[email] });
  if (now) firstSeen.set(context, now);
  return context.newPage();
}

/** A setup write the test makes through the API (not the screen under test) applies to whatever revision the record has:
 *  the e2e servers require If-Match from a signed-in session (SOFTWARDEN_REQUIRE_IF_MATCH), and `*` matches any
 *  revision (RFC 9110). A screen's form sends the ETag it read. */
export const ANY_REVISION_FOR = (method: string): Record<string, string> => (["PATCH", "DELETE"].includes(method.toUpperCase()) ? { "If-Match": "*" } : {});

export async function post(page: Page, path: string, body: unknown, method = "POST") {
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const res = await page.request.fetch(`/api/v1/${path}`, { method, data: body, headers: { "X-CSRFToken": csrf, ...ANY_REVISION_FOR(method) } });
  expect(res.ok(), await res.text()).toBeTruthy();
  return res.status() === 204 ? null : res.json();
}

/** The link of the newest invitation emailed to `to`, read from the e2e server's in-memory outbox (POST e2e/outbox,
 *  as an Admin `page`) and taken out of it. Where more than one organization shares the installation, only this emailed
 *  link creates an account, and only it says whether the address has one; the link the Admin is shown joins an
 *  existing account signed in as itself. Returns the path, `/invite#<token>`. */
export async function emailedInvitation(admin: Page, to: string): Promise<string> {
  const mail: { body: string }[] = await post(admin, "e2e/outbox", { to, clear: true });
  const links = mail.map((m) => m.body.match(/\/invite#[\w-]+/)?.[0]).filter((l): l is string => !!l);
  expect(links.length, `an invitation emailed to ${to}`).toBeGreaterThan(0);
  return links[links.length - 1];
}

/** Needs attention lists its all-clear queues on one line; open it so each one's sentence shows. Call it
 *  once the queues have loaded. */
export async function showAllClear(page: Page) {
  const button = page.getByRole("button", { name: /^Nothing waiting \(/ });
  // Loaded: the jump links (some queue has items) or the Nothing waiting line (every queue is clear).
  await expect(page.getByRole("navigation", { name: "Queues" }).or(button).first()).toBeVisible();
  if (await button.count() && (await button.getAttribute("aria-expanded")) === "false") await button.click();
}

export async function personId(page: Page, name: string): Promise<string> {
  const people: { id: string; name: string }[] = await (await page.request.get("/api/v1/people")).json();
  return people.find((p) => p.name === name)!.id;
}

/** A unique name prefix. Both parts have a fixed width (13-digit milliseconds, a 3-digit random part), so one token can
 *  never be the prefix of another and a name search for one never matches the other's records. */
export const token = () => `Dt${Date.now()}${String(Math.floor(Math.random() * 1000)).padStart(3, "0")}`;

/** A small file the API's PDF content check accepts (attachments). */
export const PDF = (text = "Softwarden test") => Buffer.from(`%PDF-1.4\n% ${text}\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n`);
