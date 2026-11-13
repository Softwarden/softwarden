import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, PASSWORD, post, token } from "./helpers";

/** The smaller front-end gaps: a save is confirmed (focus on the page's heading, "Saved
 *  <name>." announced), every missing-ID route says "not found" with no Retry, links in running table text are
 *  underlined (axe link-in-text-block), preview-only systems aren't named on a default install, Back undoes a filter,
 *  tab or scope change, the phone menu closes on Escape, and the sign-in form has a pending state, ties its error to the
 *  fields and says when the connection dropped. */

test.describe.configure({ timeout: 120_000 });

const MISSING = "00000000-0000-4000-8000-00000000dead";

test("Saving an edit puts focus on the heading and announces Saved", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const app = await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active" });
  await page.goto(`/applications/${app.id}/edit`);
  await page.getByLabel("Name (required)").fill(`${t} General ledger`);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const heading = page.getByRole("heading", { level: 1, name: `${t} General ledger` });
  await expect(heading).toBeFocused();
  await expect(page.getByTestId("shell-announcer")).toHaveText(`Saved ${t} General ledger.`);
  // Shown as well as said, and not said again after a reload.
  await expect(page.getByRole("main").getByText(`${t} General ledger saved.`)).toBeVisible();
  // Said once: the page's own status region doesn't repeat what the announcer said.
  await expect(page.getByRole("status").filter({ hasText: `${t} General ledger saved.` })).toHaveCount(0);
  await page.reload();
  await expect(heading).toBeVisible();
  await expect(page.getByTestId("shell-announcer")).toHaveText("");

  // The same for an integration, whose edit form is its own.
  const other = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  const flow = await post(page, "integrations", { name: `${t} Feed`, lifecycle: "active", source: app.id, target: other.id });
  await page.goto(`/integrations/${flow.id}/edit`);
  await page.getByLabel("Name (required)").fill(`${t} Payroll feed`);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: `${t} Payroll feed` })).toBeFocused();
  await expect(page.getByTestId("shell-announcer")).toHaveText(`Saved ${t} Payroll feed.`);
  await page.context().close();
});

/** Every route that takes a record's ID, with an ID that doesn't exist. */
const MISSING_ROUTES = [
  `/applications/${MISSING}`, `/applications/${MISSING}/edit`, `/components/${MISSING}`, `/components/${MISSING}/edit`,
  `/ai-agents/${MISSING}`, `/ai-agents/${MISSING}/edit`, `/integrations/${MISSING}`, `/integrations/${MISSING}/edit`,
  `/vendors/${MISSING}`, `/assessments/${MISSING}`, `/renewals/${MISSING}`, `/renewals/${MISSING}/edit`,
  `/contracts/${MISSING}`, `/contracts/${MISSING}/edit`, `/contracts/${MISSING}/renew`, `/license-pools/${MISSING}`,
  `/license-pools/${MISSING}/edit`, `/discovery/connectors/${MISSING}`, `/discovery/setup/no-such-source`,
  `/discovery/runs/${MISSING}`, `/discovery/runs/${MISSING}/${MISSING}`, `/people/${MISSING}`, `/admin/audit/${MISSING}`,
  `/admin/notification-rules/${MISSING}`, `/admin/notification-log/${MISSING}`, `/admin/webhooks/deliveries/${MISSING}`,
  `/imports/${MISSING}`,
];

test("Every missing-ID route shows a not-found heading and no Retry", async ({ browser, baseURL }) => {
  test.setTimeout(300_000);
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const wrong: string[] = [];
  for (const route of MISSING_ROUTES) {
    await page.goto(route);
    const heading = page.getByRole("heading", { level: 1 });
    try {
      await expect(heading).toHaveText(/not found/i, { timeout: 5_000 });
    } catch {
      wrong.push(`${route}: heading ${JSON.stringify(await heading.allTextContents())}`);
      continue;
    }
    if (await page.getByRole("button", { name: /retry/i }).count()) wrong.push(`${route}: offers Retry`);
    if (!(await page.getByRole("main").getByRole("link", { name: /^Back to / }).count())) wrong.push(`${route}: no way back`);
  }
  expect(wrong).toEqual([]);
  await page.context().close();
});

test("Links in running table text are underlined on Contracts and a record's Relationships", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const t = token();
  const app = await post(page, "applications", { name: `${t} Workday`, lifecycle: "active" });
  const other = await post(page, "applications", { name: `${t} ADP`, lifecycle: "active" });
  await post(page, "integrations", { name: `${t} Payroll feed`, lifecycle: "active", source: app.id, target: other.id });
  const renewal = await post(page, "renewals", { name: `${t} Workday renewal`, auto_renew: "yes", renewal_date: "2027-06-30" });
  await post(page, "contracts", {
    name: `${t} Workday agreement`, amount: "1200.00", billing_frequency: "annual", start_date: "2026-07-01", end_date: "2027-06-30", auto_renew: "yes",
    notice_value: 30, notice_unit: "days", renewal_obligation: renewal.id,
  });

  const underlined = (l: ReturnType<typeof page.getByRole>) => l.evaluate((el) => getComputedStyle(el).textDecorationLine);
  await page.goto(`/contracts?q=${t}`);
  const renewalLink = page.getByRole("link", { name: `${t} Workday renewal` });
  await expect(renewalLink).toBeVisible();
  expect(await underlined(renewalLink)).toContain("underline");
  await expectNoViolations(page, "Contracts with a renewal link");
  await page.goto(`/applications/${app.id}?tab=relationships`);
  const peer = page.getByRole("link", { name: `${t} ADP` });
  await expect(peer).toBeVisible();
  expect(await underlined(peer)).toContain("underline");
  await expectNoViolations(page, "Relationships with an integration");
  await page.context().close();
});

test("A default install doesn't name preview systems on Connections or a renewal's Tickets", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const me = await (await page.request.get("/api/v1/auth/me")).json();
  test.skip(me.capabilities.includes("preview"), "this server turns preview features on");
  await page.goto("/discovery/connections");
  await expect(page.getByTestId("admin-only")).toBeVisible();
  await expect(page.getByTestId("admin-only")).not.toContainText("Okta");
  await expect(page.getByTestId("admin-only")).not.toContainText("Google Workspace");
  await expect(page.getByTestId("admin-only")).toContainText("Microsoft and Halo");
  await page.context().close();

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const renewal = await post(editor, "renewals", { name: `${token()} Tickets`, auto_renew: "yes", renewal_date: "2027-06-30" });
  await viewer.goto(`/renewals/${renewal.id}?tab=tickets`);
  const empty = viewer.getByText(/Editors and Admins link/);
  await expect(empty).toHaveText("Editors and Admins link Jira and other tickets here.");
  await viewer.context().close();
  await editor.context().close();
});

test("Back undoes a filter, tab or scope change; the phone menu closes on Escape", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.goto("/applications");
  await page.getByRole("group", { name: "Quick filters" }).getByRole("button", { name: "Archived" }).click();
  await expect(page).toHaveURL(/preset=archived/);
  await page.goBack();
  await expect(page).not.toHaveURL(/preset=archived/);
  await expect(page.getByRole("group", { name: "Quick filters" }).getByRole("button", { name: "Active" })).toHaveAttribute("aria-pressed", "true");

  await page.goto("/");
  await page.getByRole("group", { name: "Show items" }).getByRole("button", { name: /^All/ }).click();
  await expect(page).toHaveURL(/scope=all/);
  await page.goBack();
  await expect(page).not.toHaveURL(/scope=all/);

  await page.goto("/reports");
  await page.getByRole("tab").nth(1).click();
  await expect(page).toHaveURL(/tab=/);
  await page.goBack();
  await expect(page).not.toHaveURL(/tab=/);

  // The Notification log: a select pushes, a typed date replaces the entry it is on, and Clear pushes.
  await page.goto("/admin/notification-log");
  await page.getByLabel("Channel").selectOption({ index: 1 });
  await expect(page).toHaveURL(/channel=/);
  await page.getByLabel("Queued from").fill("2026-01-01");
  await expect(page).toHaveURL(/from=2026-01-01/);
  await page.getByLabel("Queued from").fill("2026-02-01");
  await expect(page).toHaveURL(/from=2026-02-01/);
  await page.goBack();
  // Back leaves the typed dates (they replaced the channel's entry) and the channel, in one step.
  await expect(page).toHaveURL(/\/admin\/notification-log$/);
  await expect(page.getByLabel("Channel")).toHaveValue("");

  // A typed filter chip (a date on Contracts) replaces the entry, like the search: Back leaves the page.
  await page.goto("/contracts");
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Cancellation deadline from" }).click();
  await page.locator("#contracts-filter-deadline_after").fill("2026-03-01");
  await expect(page).toHaveURL(/deadline_after=2026-03-01/);
  await page.goBack();
  await expect(page).toHaveURL(/\/admin\/notification-log$/);

  await page.setViewportSize({ width: 390, height: 800 });
  const menu = page.getByRole("button", { name: "Menu" });
  await menu.click();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link").first().focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeHidden();
  await expect(menu).toBeFocused();
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  await page.context().close();
});

test("Sign-in shows a pending state, ties its error to the fields, and names a dropped connection", async ({ browser, baseURL }) => {
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto("/login");
  await page.getByLabel("Email").fill("admin@example.test");
  await page.getByLabel("Password").fill("not-the-password");
  // Hold the request so the pending state can be seen, and count the attempts a second Enter would add.
  let attempts = 0;
  let release: () => void = () => undefined;
  const held = new Promise<void>((done) => { release = done; });
  await page.route("**/api/v1/auth/login", async (route) => { attempts += 1; await held; await route.continue(); });
  await page.getByLabel("Password").press("Enter");
  await expect(page.getByRole("button", { name: "Signing in…" })).toBeVisible();
  await page.getByLabel("Password").press("Enter");
  release();
  const error = page.getByRole("alert");
  await expect(error).toBeVisible();
  expect(attempts).toBe(1);
  await expect(page.getByLabel("Email")).toHaveAttribute("aria-describedby", "login-error");
  await expect(page.getByLabel("Password")).toHaveAttribute("aria-describedby", "login-error");
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();

  await page.unroute("**/api/v1/auth/login");
  await page.route("**/api/v1/auth/login", (route) => route.abort("internetdisconnected"));
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(error).toHaveText("Couldn't reach Softwarden, so you aren't signed in. Check your connection and try again.");
  await expectNoViolations(page, "Sign-in with an error");
  await page.context().close();
});
