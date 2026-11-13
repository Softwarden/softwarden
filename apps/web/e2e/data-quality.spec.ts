import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PDF, pageAs, personId, post, token } from "./helpers";

/** Data quality. The dashboard's Data quality panel, the /data-quality page (failing records with
 *  their filters in the URL, the checks, exceptions with add / extend-only change / remove and the owner-left case),
 *  the completeness bar, filters and ordering on record lists, a record's checks as chips with "Add exception", the
 *  Needs attention Data quality queue (rows per check and IT steward, snoozable, counted in the badge), and the
 *  Organization settings switches and attachment limits.
 *
 *  Rule switches, attachment limits and the dashboard's numbers are organization-wide, so this file runs in its own
 *  organization (the Playwright-only `e2e/spec-org` route, `quality`); its tests run one after another. */

const ADMIN = "dq-admin@example.test";
const EDITOR = "dq-editor@example.test";
const VIEWER = "dq-viewer@example.test";

test.describe.configure({ mode: "serial" });

let made: Promise<void> | null = null;
async function setup(browser: Browser, baseURL: string | undefined) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "quality" });
    await contoso.context().close();
  })();
  await made;
}

type Rec = { id: string; name: string };
type Fixture = { complete: Rec; gappy: Rec; editorId: number; t: string };

/** A record passing every check and one failing data classification, vendor and documentation, both stewarded by the
 *  Editor. */
async function records(page: Page): Promise<Fixture> {
  const t = token();
  const me = await (await page.request.get("/api/v1/auth/me")).json();
  const ines = await personId(page, "Ines Okafor");
  const mateo = await personId(page, "Mateo Silva");
  const teams: Rec[] = await (await page.request.get("/api/v1/teams")).json();
  const vendors: Rec[] = await (await page.request.get("/api/v1/vendors")).json();
  const owned = {
    lifecycle: "active", business_owner: ines, technical_owner: mateo, support_team: teams.find((x) => x.name === "Platform Team")!.id,
    criticality: "high", it_steward: me.user.id,
  };
  const complete = await post(page, "applications", {
    ...owned, name: `${t} Complete`, data_classification: "internal", vendor: vendors.find((v) => v.name === "Northwind Traders")!.id,
    documentation_url: "https://docs.example.test/complete", built_by: "vendor",
  });
  // On a line of a live contract, so the "Not on a contract" check passes for it too.
  const today = new Date();
  const iso = (days: number) => new Date(today.getTime() + days * 86_400_000).toISOString().slice(0, 10);
  const agreement = await post(page, "contracts", {
    name: `${t} Complete license`, amount: "1200.00", billing_frequency: "annual", start_date: iso(-30), end_date: iso(300), status: "active",
  });
  await post(page, `contracts/${agreement.id}/lines`, { record: complete.id, allocation_percent: null });
  const gappy = await post(page, "applications", { ...owned, name: `${t} Gappy`, built_by: "vendor" });
  return { complete, gappy, editorId: me.user.id, t };
}

let fixture: Promise<Fixture> | null = null;
async function editorWithRecords(browser: Browser, baseURL: string | undefined) {
  await setup(browser, baseURL);
  const page = await pageAs(browser, baseURL, EDITOR);
  fixture ??= records(page);
  return { page, f: await fixture };
}

test("Dashboard: the Data quality panel shows the compliance percentage, failing records by check (chart and table) and by kind", async ({ browser, baseURL }) => {
  const { page } = await editorWithRecords(browser, baseURL);
  const summary = await (await page.request.get("/api/v1/data-quality/summary")).json();
  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(page.getByRole("navigation").getByRole("link", { name: "Dashboard" })).toBeVisible(); // Work section
  const panel = page.getByRole("region", { name: "Data quality" });
  await expect(panel.getByTestId("stat-compliance-value")).toHaveText(`${summary.score}%`);
  await expect(panel.getByTestId("stat-complete-value")).toHaveText(`${summary.complete} of ${summary.records}`);
  await expect(panel.getByTestId("stat-failing-value")).toHaveText(String(summary.failing));
  await expect(panel.getByTestId("failing-chart")).toBeVisible();
  // Direct labels on the bars, and the same numbers in the table with links to the records.
  const failing = summary.by_rule.filter((r: { failing: number }) => r.failing > 0);
  for (const r of failing) {
    await expect(panel.getByTestId("failing-chart")).toContainText(r.label);
    const row = panel.getByRole("region", { name: "Data quality by check table" }).getByRole("row", { name: new RegExp(`^${r.label}`) });
    await expect(row.getByRole("link")).toHaveAttribute("href", `/data-quality?rule=${r.id}`);
    await expect(row.getByRole("link")).toContainText(String(r.failing));
  }
  const byKind = panel.getByRole("region", { name: "Data quality by kind table" });
  await expect(byKind.getByRole("row", { name: /^Applications/ })).toContainText(String(summary.by_kind.find((k: { kind: string }) => k.kind === "application").records));
  await expectNoViolations(page, "Dashboard with the Data quality panel");
  await page.context().close();
});

test("Record lists: a completeness bar, filters for completeness and a failing check, and ordering by completeness", async ({ browser, baseURL }) => {
  const { page, f } = await editorWithRecords(browser, baseURL);
  await page.goto(`/applications?q=${f.t}`);
  const table = page.getByRole("region", { name: "Applications table" });
  const gappy = table.getByRole("row", { name: new RegExp(`${f.t} Gappy`) });
  const complete = table.getByRole("row", { name: new RegExp(`${f.t} Complete`) });
  await expect(complete.locator("[data-completeness]")).toHaveAttribute("data-completeness", "100");
  await expect(complete).toContainText("100%");
  const score = Number(await gappy.locator("[data-completeness]").getAttribute("data-completeness"));
  expect(score).toBeLessThan(100);
  await expect(gappy).toContainText(`${score}%`);

  // Ordering by completeness: least complete first, in the URL.
  await table.getByRole("button", { name: "Completeness" }).click();
  await expect(page).toHaveURL(/ordering=completeness/);
  await expect(page.getByRole("columnheader", { name: "Completeness" })).toHaveAttribute("aria-sort", "ascending");
  await expect(table.getByRole("rowheader").first()).toContainText(`${f.t} Gappy`);
  await expect(page.getByText(/Sorted by completeness, least complete first/)).toBeVisible();

  // Completeness below 100: only the gappy one.
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Completeness below (%)" }).click();
  await page.getByLabel("Completeness below (%)", { exact: true }).fill("100");
  await page.getByLabel("Completeness below (%)", { exact: true }).press("Enter");
  await expect(page).toHaveURL(/completeness__lt=100/);
  await expect(table.getByRole("rowheader")).toHaveCount(1);
  await expect(table.getByRole("rowheader")).toContainText(`${f.t} Gappy`);

  // A failing check, by its label.
  await page.goto(`/applications?q=${f.t}&quality_rule=data_classification`);
  await expect(page.getByLabel("Failing check", { exact: true })).toHaveValue("data_classification");
  await expect(table.getByRole("rowheader")).toHaveCount(1);
  await expect(table.getByRole("rowheader")).toContainText(`${f.t} Gappy`);
  await expectNoViolations(page, "Applications list filtered by a failing check");
  await page.context().close();
});

test("Record detail: completeness and failing checks as chips; Add exception from a chip; the check shows not applicable", async ({ browser, baseURL }) => {
  const { page, f } = await editorWithRecords(browser, baseURL);
  await page.goto(`/applications/${f.gappy.id}`);
  await expect(page.getByTestId("record-completeness")).toContainText(/\d+%/);
  const panel = page.getByRole("region", { name: "Data quality" });
  const failing = panel.getByRole("list", { name: "Failing checks" });
  await expect(failing.locator("[data-gap]", { hasText: "No data classification" })).toBeVisible();
  await expect(failing.locator("[data-gap]", { hasText: "No vendor" })).toBeVisible();
  // Data classification can't be excepted (record the value instead); a vendor can.
  await expect(failing.getByRole("button", { name: "Add exception for No data classification" })).toHaveCount(0);
  await expectNoViolations(page, "Record detail with data-quality chips");
  const before = Number((await page.getByTestId("record-completeness").textContent())!.match(/(\d+)%/)![1]);

  await failing.getByRole("button", { name: "Add exception for No vendor" }).click();
  const dialog = page.getByRole("dialog", { name: `Add an exception — ${f.t} Gappy` });
  await expect(dialog.getByLabel("Check (required)")).toHaveValue("vendor");
  // The expiry defaults to a year ahead, the most allowed.
  const max = await dialog.getByLabel("Expires on (required)").getAttribute("max");
  await expect(dialog.getByLabel("Expires on (required)")).toHaveValue(max!);
  await dialog.getByRole("button", { name: "Add exception" }).click();
  await expect(dialog.getByLabel("Reason (required)")).toBeFocused(); // the first invalid field
  await expect(dialog.getByLabel("Reason (required)")).toHaveAccessibleDescription(/Say why this check doesn't apply/);
  await dialog.getByLabel("Reason (required)").fill("Built from an open-source project we host ourselves.");
  await dialog.getByRole("combobox", { name: "Accountable person (required)" }).fill("Ines");
  await page.getByRole("option", { name: /^Ines Okafor/ }).click();
  const soon = new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
  await dialog.getByLabel("Expires on (required)").fill(soon);
  await expectNoViolations(page, "Add exception dialog, filled");
  await dialog.getByRole("button", { name: "Add exception" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(panel.getByRole("list", { name: "Checks marked not applicable" })).toContainText("No vendor");
  await expect(failing.locator("[data-gap]", { hasText: "No vendor" })).toHaveCount(0);
  await expect.poll(async () => Number((await page.getByTestId("record-completeness").textContent())!.match(/(\d+)%/)![1])).toBeGreaterThan(before);

  // A second exception for the same check is refused with a pointer to the existing one.
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const dup = await page.request.post("/api/v1/data-quality/exceptions", { headers: { "X-CSRFToken": csrf }, data: { record: f.gappy.id, rule: "vendor", reason: "again", owner: await personId(page, "Ines Okafor") } });
  expect(dup.status()).toBe(409);
  await page.context().close();
});

test("Data quality page: failing records filtered in the URL, the checks, and a Viewer's read-only view", async ({ browser, baseURL }) => {
  const { page, f } = await editorWithRecords(browser, baseURL);
  await page.goto(`/data-quality?rule=data_classification&it_steward=${f.editorId}`);
  await expect(page.getByRole("heading", { level: 1, name: "Data quality" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Failing records" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("Check", { exact: true })).toHaveValue("data_classification");
  await expect(page.getByLabel("IT steward", { exact: true })).toHaveValue(String(f.editorId));
  const table = page.getByRole("region", { name: "Records failing data-quality checks table" });
  await expect(table.getByRole("link", { name: `${f.t} Gappy` })).toHaveAttribute("href", `/applications/${f.gappy.id}`);
  await expect(table.getByRole("link", { name: `${f.t} Complete` })).toHaveCount(0);
  await expectNoViolations(page, "Data quality, failing records filtered");

  await page.getByLabel("Kind", { exact: true }).selectOption({ label: "Components" });
  await expect(page).toHaveURL(/kind=component/);
  await expect(page.getByText("No records fail this filter")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL("/data-quality");
  await page.getByLabel("IT steward", { exact: true }).selectOption({ label: "No IT steward" });
  await expect(page).toHaveURL("/data-quality?it_steward=none");

  await page.getByRole("tab", { name: "Checks" }).click();
  await expect(page).toHaveURL("/data-quality?tab=rules");
  const rules = page.getByRole("region", { name: "Data-quality checks table" });
  await expect(rules.getByRole("row", { name: /^No business owner/ })).toContainText("Always on");
  // On (contracts exist); it was Unavailable, planned, before.
  await expect(rules.getByRole("row", { name: /^Not on a contract/ })).toContainText("✓On");
  await expect(rules.getByRole("row", { name: /^No data classification/ })).toContainText("On");
  await expectNoViolations(page, "Data quality, checks");
  await page.context().close();

  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto("/data-quality?tab=exceptions");
  await expect(viewer.getByRole("heading", { level: 1, name: "Data quality" })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Add exception" })).toHaveCount(0);
  await expect(viewer.getByRole("row", { name: new RegExp(`${f.t} Gappy`) })).toBeVisible();
  await expect(viewer.getByRole("button", { name: /^Change/ })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Remove/ })).toHaveCount(0);
  await expectNoViolations(viewer, "Data quality exceptions, Viewer");
  await viewer.goto(`/applications/${f.gappy.id}`);
  await expect(viewer.getByRole("region", { name: "Data quality" }).getByRole("button", { name: /Add exception/ })).toHaveCount(0);
  await viewer.context().close();
});

test("An expired exception is renewed from today, not from its old expiry", async ({ browser, baseURL }) => {
  const { page, f } = await editorWithRecords(browser, baseURL);
  // The API can't make an exception that has already lapsed, so the list and the edit read are rewritten to one that
  // ended last week; the PATCH is answered here too.
  const lapsed = new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10);
  const expire = (x: Record<string, unknown>) => ({ ...x, expires_on: lapsed, active: false });
  await page.route(/\/api\/v1\/data-quality\/exceptions/, async (route) => {
    if (route.request().method() === "PATCH") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({}) });
    const res = await route.fetch();
    const body = await res.json();
    const out = Array.isArray(body.results) ? { ...body, results: body.results.map(expire) } : expire(body);
    return route.fulfill({ response: res, body: JSON.stringify(out) });
  });
  await page.goto(`/data-quality?tab=exceptions&record=${f.gappy.id}`);
  const row = page.getByRole("region", { name: "Exceptions table" }).getByRole("row", { name: new RegExp(`${f.t} Gappy`) });
  await expect(row).toContainText("Expired");
  await row.getByRole("button", { name: /^Change/ }).click();
  const dialog = page.getByRole("dialog", { name: `Change the exception — ${f.t} Gappy` });
  const field = dialog.getByLabel("Expires on (required)");
  const today = await field.getAttribute("min");
  expect(today! > lapsed).toBeTruthy(); // today, not the old expiry
  await expect(field).toHaveAccessibleDescription(new RegExp(`expired on ${lapsed}; renew it from today`));
  const yesterday = new Date(Date.parse(today!) - 864e5).toISOString().slice(0, 10);
  await field.fill(yesterday);
  await dialog.getByRole("button", { name: "Save exception" }).click();
  await expect(field).toHaveAccessibleDescription(/Choose today or a later date/);
  const renewed = new Date(Date.parse(today!) + 30 * 864e5).toISOString().slice(0, 10);
  await field.fill(renewed);
  const [patch] = await Promise.all([
    page.waitForRequest((r) => r.method() === "PATCH" && r.url().includes("/api/v1/data-quality/exceptions/")),
    dialog.getByRole("button", { name: "Save exception" }).click(),
  ]);
  expect(patch.postDataJSON()).toEqual({ expires_on: renewed });
  await expect(dialog).toHaveCount(0);
  await page.unroute(/\/api\/v1\/data-quality\/exceptions/);
  await page.context().close();
});

test("Exceptions: extend-only change with If-Match, the owner-left case, and removal", async ({ browser, baseURL }) => {
  const { page, f } = await editorWithRecords(browser, baseURL);
  await page.goto(`/data-quality?tab=exceptions&record=${f.gappy.id}`);
  const row = page.getByRole("region", { name: "Exceptions table" }).getByRole("row", { name: new RegExp(`${f.t} Gappy`) });
  await expect(row).toContainText("No vendor");
  await expect(row).toContainText("Ines Okafor");
  const current = (await row.locator("time").textContent())!;

  // Extend-only: an earlier date is refused in the form.
  await row.getByRole("button", { name: /^Change/ }).click();
  const dialog = page.getByRole("dialog", { name: `Change the exception — ${f.t} Gappy` });
  await expect(dialog.getByLabel("Expires on (required)")).toHaveAttribute("min", current);
  const earlier = new Date(Date.parse(current) - 5 * 864e5).toISOString().slice(0, 10);
  await dialog.getByLabel("Expires on (required)").fill(earlier);
  await dialog.getByRole("button", { name: "Save exception" }).click();
  await expect(dialog.getByLabel("Expires on (required)")).toBeFocused();
  await expect(dialog.getByLabel("Expires on (required)")).toHaveAccessibleDescription(/or later; remove the exception to end it sooner/);
  await expectNoViolations(page, "Change exception dialog with an error");
  const later = new Date(Date.parse(current) + 10 * 864e5).toISOString().slice(0, 10);
  await dialog.getByLabel("Expires on (required)").fill(later);
  const [patch] = await Promise.all([
    page.waitForRequest((r) => r.method() === "PATCH" && r.url().includes("/api/v1/data-quality/exceptions/")),
    dialog.getByRole("button", { name: "Save exception" }).click(),
  ]);
  expect(patch.headers()["if-match"]).toMatch(/^"/);
  expect(patch.postDataJSON()).toEqual({ expires_on: later });
  await expect(dialog).toHaveCount(0);
  await expect(row.locator("time")).toHaveText(later);

  // The accountable person leaves: the exception is listed under "Owner left" and can't be extended until reassigned.
  const ines = await personId(page, "Ines Okafor");
  await post(page, `people/${ines}`, { active: false }, "PATCH");
  try {
    await page.goto("/data-quality?tab=exceptions&owner=left");
    await expect(page.getByLabel("Accountable person", { exact: true })).toHaveValue("left");
    const left = page.getByRole("region", { name: "Exceptions table" }).getByRole("row", { name: new RegExp(`${f.t} Gappy`) });
    await expect(left).toContainText("Owner left");
    await expectNoViolations(page, "Exceptions, owner left");
    await left.getByRole("button", { name: /^Change/ }).click();
    const again = page.getByRole("dialog", { name: `Change the exception — ${f.t} Gappy` });
    await expect(again).toContainText("Ines Okafor is no longer active");
    const further = new Date(Date.parse(later) + 5 * 864e5).toISOString().slice(0, 10);
    await again.getByLabel("Expires on (required)").fill(further);
    await again.getByRole("button", { name: "Save exception" }).click();
    await expect(again.getByRole("combobox", { name: "Accountable person (required)" })).toHaveAccessibleDescription(/Choose an active person to extend it/);
    await again.getByRole("combobox", { name: "Accountable person (required)" }).fill("Mateo");
    await page.getByRole("option", { name: /^Mateo Silva/ }).click();
    await again.getByRole("button", { name: "Save exception" }).click();
    await expect(again).toHaveCount(0);
    await expect(page.getByText("No exceptions match these filters")).toBeVisible(); // no longer owner-left
  } finally {
    await post(page, `people/${ines}`, { active: true }, "PATCH");
  }

  // Remove: the check counts again.
  await page.goto(`/data-quality?tab=exceptions&record=${f.gappy.id}`);
  const last = page.getByRole("region", { name: "Exceptions table" }).getByRole("row", { name: new RegExp(`${f.t} Gappy`) });
  await expect(last).toContainText("Mateo Silva");
  await last.getByRole("button", { name: /^Remove/ }).click();
  const remove = page.getByRole("dialog", { name: "Remove the exception for “No vendor”?" });
  await expectNoViolations(page, "Remove exception dialog");
  await remove.getByRole("button", { name: "Remove exception" }).click();
  await expect(remove).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "counts again" })).toBeAttached();
  await expect(page.getByText("No exceptions match these filters")).toBeVisible();
  await page.context().close();
});

test("Needs attention: Data quality rows per check and IT steward, linked to the filtered page, snoozable, and counted in the badge", async ({ browser, baseURL }) => {
  const { page, f } = await editorWithRecords(browser, baseURL);
  await page.goto("/");
  const section = page.getByRole("region", { name: /^Data quality/ });
  const row = section.getByRole("row", { name: /^No data classification/ });
  await expect(row.getByRole("link", { name: "No data classification", exact: true })).toHaveAttribute("href", `/data-quality?rule=data_classification&it_steward=${f.editorId}`);
  await expect(row).toContainText("Devi Editor");
  // The page's total and the navigation badge count the same items, Data quality included.
  const body = await (await page.request.get("/api/v1/needs-attention?scope=mine")).json();
  const total = Object.values(body).filter(Array.isArray).reduce((n: number, q) => n + (q as unknown[]).length, 0);
  expect(body.counts.mine).toBe(total);
  expect(body.data_quality.length).toBeGreaterThan(0);
  await expect(page.getByText(`${total} ${total === 1 ? "item" : "items"} derived from your catalog records.`)).toBeVisible();
  await expect(page.getByRole("button", { name: /^Assigned to me/ })).toContainText(String(total));
  const nav = await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /^Needs attention/ }).textContent();
  expect(Number(/, (\d+) items?/.exec(nav ?? "")![1])).toBe(total);
  await expect(page.getByRole("navigation", { name: "Queues" }).getByRole("link", { name: /^Data quality/ })).toContainText(String(body.data_quality.length));
  await expectNoViolations(page, "Needs attention with Data quality rows");

  // Snooze: the row stays, marked.
  await row.getByRole("button", { name: /^Notifications for No data classification/ }).click();
  await page.getByRole("menuitem", { name: "Snooze notifications…" }).click();
  const dialog = page.getByRole("dialog", { name: /^Snooze notifications for No data classification/ });
  await dialog.getByRole("button", { name: "Snooze" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row).toContainText("Snoozed until");

  await row.getByRole("link", { name: /^Fix records/ }).click();
  await expect(page).toHaveURL(`/data-quality?rule=data_classification&it_steward=${f.editorId}`);
  await expect(page.getByRole("region", { name: "Records failing data-quality checks table" }).getByRole("link", { name: `${f.t} Gappy` })).toBeVisible();
  await page.context().close();
});

test("Organization settings: data-quality switches (gap checks always on) and tighter attachment limits, checked before upload", async ({ browser, baseURL }) => {
  const { f } = await editorWithRecords(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  await admin.goto("/admin/organization");
  const rules = admin.getByRole("region", { name: "Data-quality checks" }).or(admin.locator("#data-quality-rules"));
  await expect(rules.getByLabel("No business owner")).toBeChecked();
  await expect(rules.getByLabel("No business owner")).toBeDisabled();
  await expect(rules.getByLabel("No business owner")).toHaveAccessibleDescription(/Always on: it is also an ownership gap/);
  await expect(rules.getByLabel("Not on a contract")).toBeEnabled(); // an ordinary check, switchable
  await rules.getByLabel("No vendor").uncheck();
  await rules.getByRole("button", { name: "Save data-quality checks" }).click();
  await expect(admin.getByRole("status").filter({ hasText: "1 turned off" })).toBeAttached();
  await expectNoViolations(admin, "Organization settings with the data-quality and attachment sections");
  try {
    await admin.goto("/data-quality?tab=rules");
    await expect(admin.getByRole("region", { name: "Data-quality checks table" }).getByRole("row", { name: /^No vendor/ })).toContainText("Off");
    await admin.goto(`/applications/${f.gappy.id}`);
    await expect(admin.getByRole("region", { name: "Data quality" }).getByRole("list", { name: "Failing checks" })).not.toContainText("No vendor");
  } finally {
    await admin.goto("/admin/organization");
    await admin.locator("#data-quality-rules").getByLabel("No vendor").check();
    await admin.locator("#data-quality-rules").getByRole("button", { name: "Save data-quality checks" }).click();
    await expect(admin.getByRole("status").filter({ hasText: "every available check is on" })).toBeAttached();
  }

  // Attachment limits: tighter than the installation's; the upload dialog checks them before sending.
  const limits = admin.locator("#attachment-limits");
  await expect(admin.getByTestId("attachment-usage")).toContainText("files up to 25 MB");
  await limits.getByLabel("Largest file (MB)").fill("30");
  await limits.getByRole("button", { name: "Save attachment limits" }).click();
  await expect(limits.getByLabel("Largest file (MB)")).toBeFocused();
  await expect(limits.getByLabel("Largest file (MB)")).toHaveAccessibleDescription(/from 1 to 25/);
  await limits.getByLabel("Largest file (MB)").fill("1");
  await limits.getByRole("group", { name: "Allowed file types" }).getByRole("checkbox", { name: "zip" }).uncheck();
  await limits.getByRole("button", { name: "Save attachment limits" }).click();
  await expect(admin.getByTestId("attachment-usage")).toContainText("files up to 1 MB");
  try {
    await admin.goto(`/applications/${f.gappy.id}?tab=attachments`);
    await expect(admin.getByTestId("attachment-limits")).toContainText("Files up to 1 MB");
    await admin.getByRole("button", { name: "Upload file" }).click();
    const dialog = admin.getByRole("dialog", { name: /^Upload a file/ });
    // Just over the limit: one decimal place and the bytes, never "1 MB, larger than 1 MB".
    await dialog.getByLabel("File (required)").setInputFiles({ name: "edge.pdf", mimeType: "application/pdf", buffer: Buffer.concat([PDF(), Buffer.alloc(1024 * 1024, 32)]) });
    await expect(dialog.getByText(/The file is 1\.0 MB \([\d,]+ bytes\), larger than this organization's 1 MB limit/).first()).toBeVisible();
    await dialog.getByLabel("File (required)").setInputFiles({ name: "big.pdf", mimeType: "application/pdf", buffer: Buffer.concat([PDF(), Buffer.alloc(2 * 1024 * 1024, 32)]) });
    await expect(dialog.getByText(/larger than this organization's 1 MB limit/).first()).toBeVisible();
    await dialog.getByLabel("File (required)").setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: Buffer.from("PK\u0005\u0006") });
    await expect(dialog.getByText(/\.zip files can't be attached here/).first()).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
  } finally {
    await admin.goto("/admin/organization");
    await limits.getByLabel("Largest file (MB)").fill("");
    await limits.getByRole("group", { name: "Allowed file types" }).getByRole("checkbox", { name: "zip" }).check();
    await limits.getByRole("button", { name: "Save attachment limits" }).click();
    await expect(admin.getByTestId("attachment-usage")).toContainText("files up to 25 MB");
  }
  await admin.context().close();
});

test("Error states: the dashboard, the data quality tabs and search say what failed and offer Retry", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const page = await pageAs(browser, baseURL, VIEWER);
  const fail = (route: import("@playwright/test").Route) => route.fulfill({ status: 500, contentType: "application/problem+json", body: JSON.stringify({ detail: "The server had a problem." }) });
  // The dashboard (its Data quality block included) is one request, GET /dashboard.
  await page.route("**/api/v1/dashboard", fail);
  await page.route("**/api/v1/data-quality/**", fail);
  await page.goto("/dashboard");
  await expect(page.getByRole("alert")).toContainText("The server had a problem. Nothing was changed.");
  await expect(page.getByRole("alert").getByRole("button", { name: "Retry" })).toBeVisible();
  await expectNoViolations(page, "Dashboard, error");
  await page.unroute("**/api/v1/dashboard");
  await page.goto("/data-quality");
  await expect(page.getByRole("alert").first()).toContainText("The server had a problem.");
  await page.goto("/data-quality?tab=exceptions");
  await expect(page.getByRole("alert").first()).toContainText("The server had a problem.");
  await expectNoViolations(page, "Data quality exceptions, error");
  await page.goto("/data-quality?tab=rules");
  await expect(page.getByRole("alert").first()).toContainText("The server had a problem.");
  await page.unroute("**/api/v1/data-quality/**");
  await page.route("**/api/v1/search?**", fail);
  await page.goto("/search?q=anything");
  await expect(page.getByRole("alert")).toContainText("The server had a problem.");
  await expectNoViolations(page, "Search, error");
  await page.context().close();
});
