import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Licence pools. Utilisation (a bar plus words) and overuse, the add form, the proposed seat count
 *  review (apply, dismiss with a reason, dismissed history, SKUs no pool tracks), the Needs attention "Seat counts"
 *  queue, the read-only Viewer, and axe in both themes.
 *
 *  Observed seat counts are synthetic discovery observations of kind `license`, recorded through the Playwright-only
 *  `e2e/license-observation` route; they are organization-wide, so this file runs in the contracts spec organization
 *  (`e2e/spec-org`, `contracts`), with every SKU ID unique to its test. */

const ADMIN = "ct-admin@example.test";
const EDITOR = "ct-editor@example.test";
const VIEWER = "ct-viewer@example.test";

test.describe.configure({ mode: "serial" });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "contracts" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const said = (page: Page, text: string) => expect(page.getByRole("status").filter({ hasText: text })).toHaveCount(1);
async function poolOf(page: Page, t: string, purchased: number, assigned: number, sku = `${t.toLowerCase()}-sku`) {
  const me = await (await page.request.get("/api/v1/auth/me")).json();
  const app = await post(page, "applications", { name: `${t} Office`, lifecycle: "active", it_steward: me.user.id });
  const pool = await post(page, "license-pools", { application: app.id, sku_name: `${t} E5`, sku_id: sku, seats_purchased: purchased, seats_assigned: assigned });
  return { app, pool, sku };
}
const observe = (admin: Page, sku: string, purchased: number, assigned: number) =>
  post(admin, "e2e/license-observation", { sku_id: sku, sku_part_number: sku.toUpperCase(), seats_purchased: purchased, seats_assigned: assigned });

test("Utilization and overuse: the totals, the list and the pool page say it in words", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  const { pool } = await poolOf(page, t, 100, 120);
  await page.goto(`/license-pools?q=${t}`);
  await expect(page.getByRole("heading", { level: 1, name: "License pools" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Seat totals" }).getByTestId("stat-overused-value")).not.toHaveText("0");
  const row = page.getByRole("region", { name: "License pools table" }).getByRole("row", { name: new RegExp(`${t} E5`) });
  await expect(row).toContainText("120 of 100 seats (120%)");
  await expect(row).toContainText("Overused by 20 seats");
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Overuse" }).click();
  await page.getByLabel("Overuse", { exact: true }).selectOption("true");
  await expect(page).toHaveURL(/overused=true/);
  await expect(row).toBeVisible();
  await expectNoViolations(page, "License pools list");

  await row.getByRole("link", { name: `${t} E5` }).click();
  await expect(page.getByRole("heading", { level: 1, name: `${t} E5` })).toBeVisible();
  const seats = page.getByRole("region", { name: "Seats" });
  await expect(seats).toContainText("120 of 100 seats (120%)");
  await expect(seats).toContainText("Overused by 20 seats");
  await expect(page.getByRole("region", { name: "Pool" })).toContainText("Seat counts are entered by hand");
  await expectNoViolations(page, "License pool page");
  expect(pool.overused).toBe(true);
});

test("Add a license pool: seat sync needs a SKU ID; the last sync date is read-only", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  await post(page, "applications", { name: `${t} Visio`, lifecycle: "active" });
  await page.goto("/license-pools/new");
  await page.getByRole("combobox", { name: "Application (required)" }).fill(`${t} Visio`);
  await page.getByRole("option", { name: new RegExp(`${t} Visio`) }).click();
  await page.getByLabel("SKU name (required)").fill(`${t} Visio Plan 2`);
  await page.getByLabel("Seats purchased").fill("10");
  await page.getByLabel("Seats assigned").fill("4");
  await page.getByRole("checkbox", { name: /Sync seat counts/ }).check();
  await page.getByRole("button", { name: "Add license pool" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Set the SKU ID to sync seat counts from Entra." })).toBeVisible();
  await expectNoViolations(page, "License pool form with an error");
  await page.getByLabel("SKU ID").fill(`${t.toLowerCase()}-visio`);
  await page.getByRole("button", { name: "Add license pool" }).click();
  await expect(page.getByRole("heading", { level: 1, name: `${t} Visio Plan 2` })).toBeVisible();
  // Shown on the page and said once, by the shell's announcer.
  await expect(page.getByRole("main").getByText("License pool added.")).toBeVisible();
  await expect(page.getByTestId("shell-announcer")).toContainText("License pool added.");
  await expect(page.getByRole("region", { name: "Pool" })).toContainText("Not synced yet");
  await page.getByRole("link", { name: "Edit" }).click();
  await expect(page.locator("#seat_sync-help")).toContainText("Last synced: never (set by discovery only).");
});

test("Proposed seat counts: apply the source's counts, dismiss another with a reason, and see it in the dismissed history", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const page = await pageAs(browser, baseURL, EDITOR);
  const t = token();
  const one = await poolOf(page, t, 100, 90);
  await observe(admin, one.sku, 120, 95);

  await page.goto(`/license-pools/${one.pool.id}`);
  await expect(page.getByRole("note").filter({ hasText: "A source reports other seat counts." })).toBeVisible();
  await page.getByRole("tab", { name: /Proposed seat counts/ }).click();
  const table = page.getByRole("region", { name: "Proposed seat counts table" });
  await expect(table).toContainText("Seats purchased: 100 in the pool, 120 reported");
  await expect(table).toContainText("Seats assigned: 90 in the pool, 95 reported");
  await expectNoViolations(page, "Pool seat review");
  await table.getByRole("button", { name: /^Apply…/ }).click();
  const apply = page.getByRole("dialog", { name: `Apply the reported counts to ${t} E5` });
  await expect(apply.getByRole("checkbox", { name: "Seats purchased: 100 to 120" })).toBeChecked();
  await expectNoViolations(page, "Apply seat counts dialog");
  await apply.getByRole("button", { name: "Apply counts" }).click();
  await said(page, `${t} E5 now has seats purchased 120 and seats assigned 95, from Entra licenses (synthetic).`);
  await expect(page.getByRole("main").getByText("No proposed seat counts to review")).toBeVisible();
  await page.getByRole("tab", { name: "Overview" }).click();
  await expect(page.getByRole("region", { name: "Seats" })).toContainText("95 of 120 seats");
  await page.getByRole("tab", { name: "History" }).click();
  await expect(page.getByRole("checkbox", { name: "Only this record" })).toHaveCount(0); // a pool has no children
  await expect(page.getByRole("region", { name: "Audit activity table" }).getByRole("row")).not.toHaveCount(0);

  const t2 = token();
  const two = await poolOf(page, t2, 50, 40);
  await observe(admin, two.sku, 50, 45);
  await page.goto(`/license-pools/${two.pool.id}?tab=review`);
  await page.getByRole("button", { name: /^Dismiss…/ }).click();
  const dismiss = page.getByRole("dialog", { name: `Dismiss the counts for ${t2} E5` });
  await dismiss.getByRole("button", { name: "Dismiss proposal" }).click();
  await expect(dismiss.locator("#reason-error")).toContainText("Say why");
  await expectNoViolations(page, "Dismiss seat counts dialog");
  await dismiss.getByLabel("Reason (required)").fill("Five seats are contractors on a separate order.");
  await dismiss.getByRole("button", { name: "Dismiss proposal" }).click();
  await said(page, `Dismissed: ${t2} E5 keeps its counts.`);
  await page.getByRole("button", { name: "Dismissed", exact: true }).click();
  await expect(page).toHaveURL(/seats=dismissed/);
  const dismissed = page.getByRole("region", { name: "Proposed seat counts table" });
  await expect(dismissed).toContainText("Dismissed");
  await expect(dismissed).toContainText("Five seats are contractors on a separate order.");
  await expect(dismissed.getByRole("button", { name: /^Apply…/ })).toHaveCount(0);

  // The organisation-wide review lists SKUs no pool tracks, each a proposal.
  const lone = `${t.toLowerCase()}-lone`;
  await observe(admin, lone, 7, 3);
  await page.goto("/license-pools?tab=review");
  const unbound = page.getByRole("region", { name: "Unbound SKUs table" });
  await expect(unbound.getByRole("row", { name: new RegExp(lone.toUpperCase()) })).toContainText("3 of 7 assigned");
  await expectNoViolations(page, "License pools review tab");
});

test("Needs attention lists pools with proposed seat counts and links to their review", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const page = await pageAs(browser, baseURL, EDITOR);
  const t = token();
  const { pool, sku } = await poolOf(page, t, 30, 30);
  await observe(admin, sku, 35, 30);
  await page.goto("/?scope=all");
  const queue = page.getByRole("region", { name: /Seat counts/ });
  const row = queue.getByRole("row", { name: new RegExp(`${t} E5`) });
  await expect(row).toContainText(`${t} Office`);
  await expect(row).toContainText("Entra licenses (synthetic) reports 35 purchased; the pool doesn't match.");
  await expectNoViolations(page, "Needs attention with the Seat counts queue");
  // Counted with every other queue: the page's total is the badge's All count.
  const body = await (await page.request.get("/api/v1/needs-attention?scope=all")).json();
  const rows = Object.values(body).filter(Array.isArray).reduce((n: number, q) => n + (q as unknown[]).length, 0);
  expect(body.counts.all).toBe(rows);
  await row.getByRole("link", { name: new RegExp(`Review seat counts for ${t} E5`) }).click();
  await expect(page).toHaveURL(new RegExp(`/license-pools/${pool.id}\\?tab=review`));
  await expect(page.getByRole("region", { name: "Proposed seat counts table" })).toContainText("35 reported");
});

test("The Viewer reads pools and proposals but can't change them", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const editor = await pageAs(browser, baseURL, EDITOR);
  const t = token();
  const { pool, sku } = await poolOf(editor, t, 10, 12);
  await observe(admin, sku, 12, 12);
  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto(`/license-pools/${pool.id}?tab=review`);
  await expect(viewer.getByRole("heading", { level: 1, name: `${t} E5` })).toBeVisible();
  await expect(viewer.getByRole("region", { name: "Proposed seat counts table" })).toContainText("12 reported");
  await expect(viewer.getByRole("button", { name: /^Apply…/ })).toHaveCount(0);
  await expect(viewer.getByRole("link", { name: "Edit" })).toHaveCount(0);
  await expectNoViolations(viewer, "Pool review as a Viewer");
  await viewer.goto("/license-pools");
  await expect(viewer.getByRole("link", { name: "Add license pool" })).toHaveCount(0);
  await viewer.goto("/license-pools/new");
  await expect(viewer.getByRole("heading", { level: 1, name: "You can't change license pools" })).toBeVisible();
  await viewer.goto("/");
  await expect(viewer.getByRole("navigation").getByRole("link", { name: "License pools" })).toBeVisible();
  await expect(viewer.getByRole("navigation").getByRole("link", { name: "Contracts" })).toBeVisible();
});
