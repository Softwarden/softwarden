import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs } from "./helpers";

/** A bad answer from the API, or a render exception in one page, never blanks the application.
 *  - A 2xx whose body isn't JSON (a reverse proxy, SSO gateway or captive portal answering /api/* with an HTML 200) is a
 *    Problem from the API client, so every screen shows its own error state.
 *  - A page that throws while rendering (here: data of a shape it doesn't expect, as an old tab after an upgrade might
 *    get) shows "Something went wrong on this page" inside the shell, which stays usable; leaving the page recovers.
 *  - A throw outside the pages (the shell itself) shows the top-level error state instead of an empty #root. */

/** The session reads keep their real answers, so the shell signs in; every other API GET is answered by `answer`. */
const SESSION = /\/api\/v1\/(auth\/(me|csrf)|platform\/session)(\?|$)/;
async function answerGets(page: Page, answer: { contentType: string; body: string }, only?: RegExp) {
  await page.route("**/api/v1/**", (route) => {
    const req = route.request();
    if (req.method() !== "GET" || SESSION.test(req.url()) || (only && !only.test(req.url()))) return route.continue();
    return route.fulfill({ status: 200, ...answer });
  });
}
const HTML_200 = { contentType: "text/html", body: "<html><body>Sign in to the network</body></html>" };

/** Uncaught exceptions on the page. Only the HTML-200 test checks for none: React in development also reports an error
 *  a boundary caught to the window, so the render-exception tests can't use it. */
function collectPageErrors(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  return errors;
}

test("an HTML 200 from every API read shows each screen's error, never an empty page", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const errors = collectPageErrors(admin);
  await answerGets(admin, HTML_200);
  for (const path of ["/", "/applications", "/admin/system"]) {
    await admin.goto(path);
    // The client's Problem for the unreadable answer, as the screen's own error state shows it.
    await expect(admin.getByRole("alert").filter({ hasText: /wasn't readable/ }).first()).toBeVisible({ timeout: 15_000 });
    expect(await admin.evaluate(() => document.getElementById("root")?.children.length ?? 0)).toBeGreaterThan(0);
    // The shell survives: its navigation is still there.
    await expect(admin.getByRole("navigation").first()).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("api() and apiWithEtag() reject a non-JSON 2xx with a Problem, and still resolve JSON and an empty 204", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.route("**/api/v1/e2e-client/**", (route) => {
    const url = route.request().url();
    if (url.endsWith("/html")) return route.fulfill({ status: 200, contentType: "text/html", body: "<html>portal</html>" });
    if (url.endsWith("/bad-json")) return route.fulfill({ status: 200, contentType: "application/json", body: "{not json" });
    if (url.endsWith("/write")) return route.fulfill({ status: 200, contentType: "text/html", body: "<html>portal</html>" });
    if (url.endsWith("/json")) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }), headers: { ETag: '"v1"' } });
    return route.fulfill({ status: 204 });
  });
  await admin.goto("/profile");
  await expect(admin.getByRole("heading", { level: 1 })).toBeVisible();
  const result = await admin.evaluate(async () => {
    // The module the app itself runs, from the dev server (the API client's send() and readWithEtag()).
    const source = "/src/api.ts";
    const client = await import(/* @vite-ignore */ source);
    const outcome = async (p: Promise<unknown>) => {
      try {
        return { resolved: await p };
      } catch (e) {
        return { problem: e instanceof client.Problem, status: (e as { status?: number }).status, detail: (e as { detail?: string }).detail };
      }
    };
    return {
      html: await outcome(client.api("e2e-client/html")),
      badJson: await outcome(client.api("e2e-client/bad-json")),
      etagHtml: await outcome(client.apiWithEtag("e2e-client/html")),
      json: await outcome(client.api("e2e-client/json")),
      etagJson: await outcome(client.apiWithEtag("e2e-client/json")),
      empty: await outcome(client.api("e2e-client/empty")),
      write: await outcome(client.api("e2e-client/write", { method: "POST", body: {} })),
    };
  });
  for (const key of ["html", "badJson", "etagHtml"] as const) {
    expect(result[key], key).toMatchObject({ problem: true, status: 200 });
    expect((result[key] as { detail: string }).detail, key).toMatch(/wasn't readable/);
  }
  expect(result.json).toEqual({ resolved: { ok: true } });
  expect(result.etagJson).toEqual({ resolved: { data: { ok: true }, etag: '"v1"' } });
  expect(result.empty).toEqual({});
  // A write whose answer can't be read may or may not have been saved, and says so; a read doesn't.
  expect(result.write).toMatchObject({ problem: true, status: 200 });
  expect((result.write as { detail: string }).detail).toMatch(/may not have been saved/);
  expect((result.html as { detail: string }).detail).not.toMatch(/saved/);
});

test("a page that throws while rendering shows a recoverable error inside the shell, in light and dark", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  // Valid JSON of the wrong shape for the tag list (an object where a list belongs): the Applications page can't render it.
  await answerGets(admin, { contentType: "application/json", body: JSON.stringify({ unexpected: true }) }, /\/api\/v1\/tags(\?|$)/);
  await admin.goto("/applications");
  const alert = admin.getByRole("alert").filter({ hasText: "This page couldn't be shown." });
  await expect(alert).toBeVisible({ timeout: 15_000 });
  // The heading takes focus, as a new page's would.
  await expect(admin.getByRole("heading", { level: 1, name: "Something went wrong on this page" })).toBeFocused();
  await expect(admin.getByRole("navigation").first()).toBeVisible();
  await expect(alert.getByRole("button", { name: "Reload the page" })).toBeVisible();
  await expectNoViolations(admin, "the page error state");
  // Leaving the page recovers without a reload.
  await alert.getByRole("link", { name: "Go to Needs attention" }).click();
  await expect(admin).toHaveURL(/\/$/);
  await expect(admin.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  await expect(alert).toHaveCount(0);

  // Navigating to the same address clears the error too: once the answer is right again, the sidebar's link to the page
  // that failed shows it, without a reload (a reset keyed by the path alone would keep the error on screen). People &
  // Teams reads its list uncached (useLoad), so the retry asks the API again.
  await admin.unrouteAll({ behavior: "ignoreErrors" });
  await answerGets(admin, { contentType: "application/json", body: JSON.stringify({ unexpected: true }) }, /\/api\/v1\/people(\?|$)/);
  await admin.goto("/admin/people");
  await expect(alert).toBeVisible({ timeout: 15_000 });
  await admin.unrouteAll({ behavior: "ignoreErrors" });
  await admin.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "People & Teams" }).click();
  await expect(admin).toHaveURL(/\/admin\/people$/);
  await expect(admin.getByRole("heading", { level: 1, name: "People & Teams" })).toBeVisible();
  await expect(alert).toHaveCount(0);
});

test("a throw in the shell itself shows the top-level error state, not an empty page", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  // auth/me answers JSON of a shape the shell can't use (an old tab after an upgrade).
  await admin.route("**/api/v1/auth/me", (route) => route.request().method() === "GET"
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ user: { id: 1 }, organization: { id: "old-shape" } }) })
    : route.continue());
  await admin.goto("/applications");
  const alert = admin.getByRole("alert").filter({ hasText: "Something went wrong" });
  await expect(alert).toBeVisible({ timeout: 15_000 });
  expect(await admin.evaluate(() => document.getElementById("root")?.children.length ?? 0)).toBeGreaterThan(0);
  await expect(alert.getByRole("button", { name: "Reload the page" })).toBeVisible();
  await expectNoViolations(admin, "the application error state");
});
