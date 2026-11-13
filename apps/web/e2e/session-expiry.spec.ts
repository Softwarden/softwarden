import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PASSWORD, pageAs, token } from "./helpers";

/** A session that ends under a signed-in page asks to sign in again over the page, which
 *  keeps what was typed, and after signing in the page (or the page a link asked for) loads; a first `auth/me` that gets
 *  no clear answer (a 503 during a deploy) is tried again instead of showing the sign-in page to someone signed in. */

/** The session ends the way it does in a browser left overnight: the cookie is gone. */
async function endSession(page: Page) {
  await page.context().clearCookies({ name: "sessionid" });
}

test("A session that ends under a form asks to sign in again over it; the form keeps its values and saves, and a link opens its page after signing in", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const name = `${token()} Session`;
  await page.goto("/applications/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add application" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(name);
  await page.getByLabel("Version").fill("2026.11");

  await endSession(page);
  await page.getByRole("button", { name: "Save" }).click();
  const dialog = page.getByRole("dialog", { name: "Your session ended" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("admin@example.test");
  await expect(dialog.getByLabel("Password")).toBeFocused();
  // The page behind is hidden by an opaque overlay (still mounted: its values are checked below).
  const overlay = await page.locator('[data-slot="dialog-overlay"]').evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(overlay).not.toMatch(/^rgba\(.*,\s*0?\.\d+\)$|transparent/);
  await expectNoViolations(page, "Session ended dialog over a form");
  // A wrong password says so and keeps the dialog.
  await dialog.getByLabel("Password").fill("not-the-password");
  await dialog.getByRole("button", { name: "Sign in again" }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog.getByLabel("Password")).toHaveAttribute("aria-describedby", "session-password-error");
  await dialog.getByLabel("Password").fill(PASSWORD);
  await dialog.getByRole("button", { name: "Sign in again" }).click();
  await expect(dialog).toHaveCount(0);
  // Nothing typed was lost, and saving again works.
  await expect(page.getByLabel("Name (required)")).toHaveValue(name);
  await expect(page.getByLabel("Version")).toHaveValue("2026.11");
  // Focus is back in the page where it was when the dialog opened (the form's own error, or Save), not lost.
  expect(await page.evaluate(() => document.activeElement !== document.body && !!document.activeElement?.closest("main"))).toBe(true);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/applications\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();

  // The session ends again; a link in the sidebar asks to sign in, then shows the page it was for.
  await endSession(page);
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Renewals" }).click();
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: "Sign in to Softwarden" })).toHaveCount(0);
  await dialog.getByLabel("Password").fill(PASSWORD);
  await dialog.getByRole("button", { name: "Sign in again" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(/\/renewals$/);
  await expect(page.getByRole("heading", { level: 1, name: "Renewals" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0); // the refused load was read again
  await page.context().close();
});

test("A refusal while this browser is now signed in as another account moves the tab to that account's home, saying so", async ({ browser, baseURL }) => {
  // Its own sign-in (signing out below must not end the session the other specs share).
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto("/login");
  await page.getByLabel("Email").fill("editor@example.test");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await page.goto("/applications/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add application" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(`${token()} Other account`);
  // Another tab of this browser (the same cookies) signs out and in as Vic Viewer.
  const other = await page.context().newPage();
  const csrf = async () => (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  await other.request.post("/api/v1/auth/logout", { headers: { "X-CSRFToken": await csrf() } });
  await other.request.get("/api/v1/auth/csrf");
  const login = await other.request.post("/api/v1/auth/login", { data: { email: "viewer@example.test", password: PASSWORD }, headers: { "X-CSRFToken": await csrf() } });
  expect(login.ok(), await login.text()).toBeTruthy();
  await other.close();
  // This tab's save is refused as not signed in (as a request carrying the old session key is); the session, read
  // again, is Vic's: no "sign in again" over a form filled as Sam, but Vic's home and why.
  await page.route("**/api/v1/applications", (route) => route.fulfill(NOT_SIGNED_IN), { times: 1 });
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(page.getByTestId("session-notice")).toContainText("This browser is now signed in as Vic Viewer");
  await expect(page.getByRole("dialog", { name: "Your session ended" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Vic Viewer · Viewer/ })).toBeVisible();
  await page.context().close();
});

const NOT_SIGNED_IN = { status: 403, contentType: "application/problem+json", body: JSON.stringify({ type: "/problems/not-signed-in", title: "Not signed in", status: 403, detail: "Authentication credentials were not provided." }) };

test("Sign out from the dialog goes to the sign-in page, which returns to the page after signing in", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "viewer@example.test");
  await page.goto("/renewals");
  await expect(page.getByRole("heading", { level: 1, name: "Renewals" })).toBeVisible();
  await endSession(page);
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications" }).click();
  const dialog = page.getByRole("dialog", { name: "Your session ended" });
  await dialog.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Sign in to Softwarden" })).toBeVisible();
  await page.getByLabel("Email").fill("viewer@example.test");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/applications$/);
  await page.context().close();
});

test("A first session check answered 503 once is tried again: the signed-in person lands on the page they asked for, never on the sign-in form", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  // Note any sign-in form the page ever shows, however briefly.
  await page.addInitScript(() => {
    new MutationObserver(() => {
      if ([...document.querySelectorAll("h1")].some((h) => h.textContent === "Sign in to Softwarden")) (window as unknown as { sawSignIn: boolean }).sawSignIn = true;
    }).observe(document, { childList: true, subtree: true });
  });
  await page.route("**/api/v1/auth/me", (route) => route.fulfill(UNAVAILABLE), { times: 1 });
  await page.goto("/renewals?status=open");
  await expect(page.getByRole("heading", { level: 1, name: "Renewals" })).toBeVisible();
  await expect(page).toHaveURL(/\/renewals\?status=open$/);
  expect(await page.evaluate(() => (window as unknown as { sawSignIn?: boolean }).sawSignIn ?? false)).toBe(false);
  await page.context().close();
});

test("While the server doesn't answer, the page says so and keeps trying; Try now checks at once", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.route("**/api/v1/auth/me", (route) => route.fulfill(UNAVAILABLE));
  await page.goto("/renewals");
  await expect(page.getByRole("heading", { level: 1, name: "Softwarden isn't reachable right now" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText(/Trying again in \d+ seconds?\./);
  await expectNoViolations(page, "Softwarden isn't reachable");
  await page.unroute("**/api/v1/auth/me");
  await page.getByRole("button", { name: "Try now" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Renewals" })).toBeVisible();
  await page.context().close();
});

const UNAVAILABLE = { status: 503, contentType: "application/problem+json", body: JSON.stringify({ type: "about:blank", title: "Service Unavailable", status: 503, detail: "Service unavailable." }) };
