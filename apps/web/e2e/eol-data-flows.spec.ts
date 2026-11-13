import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post } from "./helpers";

/** The end-of-life calendar and the data-flow map in the SPA. The calendar's timeline and its filters in the
 *  URL with CSV; a record's End of life panel read by a Viewer and linked, changed and unlinked by an Editor; the
 *  data-flow map's table and filters in the URL, CSV, the graph (weights, risky ends, legend, the truncation notice),
 *  the "vendor risk is off" gap and no export while impersonating; an Editor setting an integration's data categories
 *  and a Viewer reading them; Needs attention's End of life and Data flows queues; the data-quality page's integration
 *  kind and flow rules; axe in both themes.
 *
 *  Both queues count in the badge and the endoflife.date cache is the installation's, so this file runs in its own
 *  organization (the Playwright-only `e2e/spec-org` route, `lifecycle`), put back before each test: PostgreSQL 12 / 14 /
 *  16 / 17 and Ubuntu 22.04 cached; Ledger DB 12.4 (past end of life), Payments API 14.2 (ends in 60 days), Intranet
 *  16.1 (supported), Mystery App 99.1 (matches no cycle), Build Server Ubuntu 22.04 (security fixes only), Untracked App
 *  17.0 (not tracked); flows Payroll export (all three risks), CRM sync and Marketing feed (clean), Bare link (no auth
 *  method, not classified). */

const ADMIN = "lf-admin@example.test";
const EDITOR = "lf-editor@example.test";
const VIEWER = "lf-viewer@example.test";

async function reset(browser: Browser, baseURL: string | undefined) {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "e2e/spec-org", { which: "lifecycle" });
  await contoso.context().close();
}
test.beforeEach(async ({ browser, baseURL }) => reset(browser, baseURL));

const said = (page: Page, text: string) => expect(page.getByRole("status").filter({ hasText: text })).toHaveCount(1);
const calendarRows = (page: Page) => page.getByRole("region", { name: "End-of-life calendar table" }).locator("[data-eol-row]");
const flowRows = (page: Page) => page.getByRole("region", { name: "Data flows table" }).locator("[data-flow-row]");
async function recordId(page: Page, path: string, name: string): Promise<string> {
  const body = await (await page.request.get(`/api/v1/${path}?q=${encodeURIComponent(name)}`)).json();
  const rows: { id: string; name: string }[] = Array.isArray(body) ? body : body.results;
  return rows.find((r) => r.name === name)!.id;
}

// ---- the end-of-life calendar ----------------------------------------------------------------------------------------

test("A Viewer reads the end-of-life calendar, filters it in the URL, exports it and reads a record's panel", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, VIEWER);
  await page.goto("/applications");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "End of life" }).click();
  await expect(page).toHaveURL(/\/end-of-life$/);
  await expect(page.getByRole("heading", { level: 1, name: "End of life" })).toBeVisible();
  await expect(calendarRows(page)).toHaveCount(5);
  // Soonest first, unknown dates last; each status a glyph and words.
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Ledger DB", "Payments API", "Intranet", "Build Server", "Mystery App"]);
  await expect(calendarRows(page).filter({ hasText: "Ledger DB" }).locator("[data-eol-status]")).toHaveText("!Past end of life");
  await expect(calendarRows(page).filter({ hasText: "Payments API" }).locator("[data-eol-status]")).toHaveText("◐Ends in 60 days");
  await expect(calendarRows(page).filter({ hasText: "Intranet" }).locator("[data-eol-status]")).toHaveText("✓Supported");
  await expect(calendarRows(page).filter({ hasText: "Build Server" }).locator("[data-eol-status]")).toHaveText("◑Security fixes only");
  // A version the API can't place is a named gap.
  await expect(calendarRows(page).filter({ hasText: "Mystery App" }).locator("[data-gap]")).toHaveText("⚠Version matches no release cycle");
  const timeline = page.getByRole("navigation", { name: "Timeline" });
  await expect(timeline.getByRole("link").first()).toHaveText(/Past end of life\s*1/);
  await expect(timeline.getByRole("link").last()).toHaveText(/No end-of-life date\s*1/);
  await expectNoViolations(page, "End-of-life calendar (Viewer)");

  // The window, in the URL: past or within 90 days.
  await page.getByLabel("Window").selectOption("90");
  await expect(page).toHaveURL(/window=90/);
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Ledger DB", "Payments API"]);
  await page.getByLabel("Window").selectOption("past");
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Ledger DB"]);
  // Product and kind, kept on reload.
  await page.getByLabel("Window").selectOption("");
  await page.getByLabel("Product").selectOption("ubuntu");
  await page.getByLabel("Kind").selectOption("component");
  await expect(page).toHaveURL(/product=ubuntu/);
  await page.reload();
  await expect(page.getByLabel("Product")).toHaveValue("ubuntu");
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Build Server"]);
  await page.getByLabel("Kind").selectOption("application");
  await expect(page.getByText("No versions match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/end-of-life$/);
  await expect(calendarRows(page)).toHaveCount(5);
  // Criticality and owner (business or technical), in the URL.
  await expect(calendarRows(page).filter({ hasText: "Ledger DB" })).toContainText("High criticality");
  await expect(calendarRows(page).filter({ hasText: "Ledger DB" })).toContainText("Business owner Ines Okafor");
  await page.getByLabel("Criticality").selectOption("critical");
  await expect(page).toHaveURL(/criticality=critical/);
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Payments API"]);
  await page.getByLabel("Criticality").selectOption("none");
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Build Server"]);
  await page.getByLabel("Criticality").selectOption("");
  await page.getByLabel("Owner").selectOption({ label: "Mateo Silva" });
  await expect(page).toHaveURL(/owner=/);
  await page.reload();
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Payments API"]);
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(calendarRows(page)).toHaveCount(5);

  // Every role exports; the file is named for today.
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/^softwarden-end-of-life-\d{4}-\d{2}-\d{2}\.csv$/);
  await said(page, "Exported the end-of-life calendar to");

  // The record's panel, read only.
  await calendarRows(page).filter({ hasText: "Ledger DB" }).getByRole("link", { name: "Ledger DB" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Ledger DB" })).toBeVisible();
  const panel = page.getByRole("region", { name: "End of life" });
  await expect(panel.locator("[data-eol-status]")).toHaveText("!Past end of life");
  await expect(panel).toContainText("PostgreSQL");
  await expect(panel).toContainText("From the application's Version field");
  await expect(panel).toContainText("Editors and Admins can change or unlink it.");
  await expect(panel.getByRole("button")).toHaveCount(0);
  await expectNoViolations(page, "Application with its End of life panel (Viewer)");
  await page.context().close();
});

test("An Editor links an application to endoflife.date, sets its cycle and unlinks it", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, EDITOR);
  const id = await recordId(page, "applications", "Untracked App");
  await page.goto(`/applications/${id}`);
  const panel = page.getByRole("region", { name: "End of life" });
  await expect(panel.getByTestId("eol-untracked")).toContainText("Not tracked.");
  const link = panel.getByRole("button", { name: "Link to endoflife.date" });
  await link.click();
  const dialog = page.getByRole("dialog", { name: "Link Untracked App to endoflife.date" });
  // The product is required.
  await dialog.getByRole("button", { name: "Link" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Choose the endoflife.date product.");
  await expect(dialog.getByRole("combobox", { name: "Product (required)" })).toBeFocused();
  await dialog.getByRole("combobox", { name: "Product (required)" }).fill("postg");
  await page.getByRole("option", { name: /PostgreSQL/ }).click();
  await expect(dialog.getByText("Leave empty to use the application's Version field (17.0).")).toBeVisible();
  await expectNoViolations(page, "Link to endoflife.date dialog");
  await dialog.getByRole("button", { name: "Link" }).click();
  await said(page, "Untracked App is linked to PostgreSQL on endoflife.date.");
  await expect(panel.locator("[data-eol-status]")).toHaveText("✓Supported");
  await expect(panel).toContainText("Worked out from the version");

  // Change: the cycles are known now the product is tracked; cycle 14 ends in 60 days.
  const change = panel.getByRole("button", { name: "Change end-of-life tracking" });
  await change.click();
  const edit = page.getByRole("dialog", { name: "Change end-of-life tracking of Untracked App" });
  await edit.getByLabel("Release cycle").selectOption("14");
  await edit.getByRole("button", { name: "Save" }).click();
  await said(page, "End-of-life tracking of Untracked App changed.");
  await expect(panel.locator("[data-eol-status]")).toHaveText("◐Ends in 60 days");
  await expect(panel).toContainText("Set by hand");
  await expect(change).toBeFocused();

  // Unlink: behind a checkbox; focus goes to Link afterwards.
  await panel.getByRole("button", { name: "Unlink from endoflife.date" }).click();
  const unlink = page.getByRole("dialog", { name: "Stop tracking Untracked App's end of life?" });
  await unlink.getByRole("button", { name: "Stop tracking Untracked App" }).click();
  await expect(unlink).toContainText("Tick the box to confirm.");
  await expectNoViolations(page, "Unlink dialog");
  await unlink.getByLabel("Remove the end-of-life tracking of Untracked App").check();
  await unlink.getByRole("button", { name: "Stop tracking Untracked App" }).click();
  await said(page, "Untracked App is no longer tracked on endoflife.date.");
  await expect(panel.getByTestId("eol-untracked")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Link to endoflife.date" })).toBeFocused();
  await page.context().close();
});

// ---- Needs attention ---------------------------------------------------------------------------------------------------

test("Needs attention lists end-of-life versions and risky data flows for their IT steward", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, EDITOR);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  const jump = page.getByRole("navigation", { name: "Queues" });
  await expect(jump.getByRole("link", { name: /^End of life\s*2$/ })).toBeVisible();
  await expect(jump.getByRole("link", { name: /^Data flows\s*2$/ })).toBeVisible();
  const eol = page.locator("[data-eol]");
  await expect(eol).toHaveCount(2);
  await expect(page.locator('[data-eol="Ledger DB"] [data-eol-status]')).toHaveText("!Past end of life");
  await expect(page.locator('[data-eol="Payments API"] [data-eol-status]')).toHaveText("◐Ends in 60 days");
  await expect(page.locator('[data-eol="Ledger DB"]').getByRole("link", { name: "Open record for Ledger DB" })).toHaveAttribute("href", /\/applications\//);
  const feed = page.locator('[data-flow="Payroll export"]');
  await expect(feed.locator("[data-risk]")).toHaveText(["!No DPA", "!No auth method", "!Down-classified"]);
  await expect(page.locator('[data-flow="Bare link"] [data-risk]')).toHaveText(["!No auth method"]);
  await expect(feed.getByRole("link", { name: "Open integration Payroll export" })).toHaveAttribute("href", /\/integrations\//);
  await expectNoViolations(page, "Needs attention with the End of life and Data flows queues");
  // The badge counts them: the page's total is the API's.
  const counts = (await (await page.request.get("/api/v1/needs-attention?scope=mine")).json()).counts;
  await expect(page.getByText(`${counts.mine} ${counts.mine === 1 ? "item" : "items"} derived from your catalog records.`)).toBeVisible();
  await page.context().close();
});

// ---- the data-flow map -------------------------------------------------------------------------------------------------

test("A Viewer reads the data-flow map: filters in the URL, CSV, the graph with its legend", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, VIEWER);
  await page.goto("/applications");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Data flows" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Data flows" })).toBeVisible();
  await expect(flowRows(page)).toHaveCount(4);
  await expect(flowRows(page).first()).toHaveAttribute("data-flow-row", "Payroll export");
  await expect(flowRows(page).first().locator("[data-risk]")).toHaveText(["!No DPA", "!No auth method", "!Down-classified"]);
  await expect(flowRows(page).first()).toContainText("Contains personal data");
  await expect(page.locator('[data-flow-row="Bare link"]').locator("[data-gap]")).toHaveText("⚠No classification");
  await expect(page.getByTestId("flows-risky-value")).toHaveText("2");
  await expect(page.getByTestId("dpa-unchecked")).toHaveCount(0);
  await expectNoViolations(page, "Data flows, table (Viewer)");

  // Filters in the URL.
  await page.getByLabel("Risk", { exact: true }).selectOption("no_dpa");
  await expect(page).toHaveURL(/risk=no_dpa/);
  await expect(flowRows(page)).toHaveCount(1);
  await page.getByLabel("Risk", { exact: true }).selectOption("");
  await page.getByLabel("Classification").selectOption("confidential");
  await expect(page).toHaveURL(/min_classification=confidential/);
  await expect(flowRows(page).locator("th[scope=row] a")).toHaveText(["Payroll export", "CRM sync"]);
  await page.getByLabel("From").selectOption({ label: "CRM" });
  await page.reload();
  await expect(page.getByLabel("From")).toHaveValue(/.+/);
  await expect(flowRows(page).locator("th[scope=row] a")).toHaveText(["CRM sync"]);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/^softwarden-data-flows-\d{4}-\d{2}-\d{2}\.csv$/);
  await said(page, "Exported the data flows to");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(flowRows(page)).toHaveCount(4);

  // The graph: one node per record at a flow's end, one edge per flow, widths by classification, risky ends marked.
  await page.getByRole("tab", { name: "Graph" }).click();
  await expect(page).toHaveURL(/view=graph/);
  const canvas = page.getByTestId("graph-canvas");
  await expect(canvas).toBeVisible();
  await expect(canvas).toHaveAttribute("aria-label", "Data-flow graph");
  const drawn = () => page.evaluate(() => {
    const cy = window.__softwardenGraph!;
    return {
      nodes: cy.nodes().map((n) => ({ label: n.data("label") as string, classes: n.classes() as unknown as string[] })),
      edges: cy.edges().map((e) => ({ label: e.data("label") as string, classes: e.classes() as unknown as string[] })),
    };
  });
  await expect.poll(async () => (await drawn()).edges.length).toBe(4);
  const { nodes, edges } = await drawn();
  expect(nodes.map((n) => n.label.split("\n")[0])).toEqual(["CRM", "HR Core", "Payroll Cloud"]);
  const payroll = edges.find((e) => e.label.includes("Personal data"))!;
  expect(payroll.classes).toEqual(expect.arrayContaining(["integration", "w4", "risky"]));
  expect(payroll.label.startsWith("! ")).toBe(true);
  expect(edges.find((e) => e.label.includes("Customer content"))!.classes).toEqual(expect.arrayContaining(["w3"]));
  const cloud = nodes.find((n) => n.label.startsWith("Payroll Cloud"))!;
  expect(cloud.classes).toContain("risky");
  expect(cloud.label).toContain("! at risk");
  const legend = page.getByRole("list", { name: "Legend" });
  await expect(legend).toContainText("Restricted data");
  await expect(legend).toContainText("A flow with a risk");
  await expect(page.getByTestId("graph-truncated")).toHaveCount(0);
  // The keyboard selects a record; its flows are listed beside the picture.
  await canvas.focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("flows-selected")).toContainText("Selected: CRM");
  await expect(page.getByTestId("flows-selected")).toContainText("CRM sync");
  await expectNoViolations(page, "Data flows, graph (Viewer)");
  await page.context().close();
});

test("The data-flow map says when the graph stops, when DPAs aren't checked, and offers no export while impersonating", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, VIEWER);
  // As the API answers when more than 1,000 flows match and when vendor risk is off.
  await page.route("**/api/v1/reports/data-flows/graph*", async (route) => {
    const body = await (await route.fetch()).json();
    await route.fulfill({ json: { ...body, truncated: true, vendor_risk: false } });
  });
  await page.route(/\/api\/v1\/reports\/data-flows(\?.*)?$/, async (route) => {
    const body = await (await route.fetch()).json();
    await route.fulfill({ json: { ...body, vendor_risk: false } });
  });
  // As `auth/me` says while platform staff act as this member.
  await page.route("**/api/v1/auth/me", async (route) => {
    const body = await (await route.fetch()).json();
    const now = Date.now();
    await route.fulfill({ json: { ...body, impersonation: {
      id: "00000000-0000-4000-8000-000000000001", by: { id: 999, name: "Platform Support", email: "support@example.test" }, reason: "Ticket 42 (synthetic)",
      started_at: new Date(now - 60_000).toISOString(), expires_at: new Date(now + 3_600_000).toISOString(),
    } } });
  });
  await page.goto("/data-flows?view=graph");
  await expect(page.getByRole("heading", { level: 1, name: "Data flows" })).toBeVisible();
  await expect(page.getByTestId("dpa-unchecked")).toContainText("DPA status not checked (vendor risk is off)");
  await expect(page.getByTestId("graph-truncated")).toContainText("The graph shows the first 4 flows");
  await expect(page.getByRole("button", { name: "Export CSV" })).toHaveCount(0);
  await expect(page.getByTestId("export-off")).toHaveText("⊘ CSV export is off while you act as this member.");
  await page.context().close();
});

test("An Editor sets an integration's data categories; a Viewer reads them and can't change them", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, EDITOR);
  const id = await recordId(editor, "integrations", "Marketing feed");
  await editor.goto(`/integrations/${id}`);
  await expect(editor.getByTestId("integration-categories")).toHaveText("None recorded");
  await editor.goto(`/integrations/${id}/edit`);
  const form = editor.getByRole("form", { name: "Edit integration" });
  const picker = form.getByRole("group", { name: "Data categories" });
  await expect(picker).toContainText("None chosen.");
  await picker.getByLabel("Personal data", { exact: true }).check();
  await picker.getByLabel("Financial", { exact: true }).check();
  await expect(picker).toContainText("Chosen: Personal data, Financial");
  await expect(picker).toContainText("(it does, as chosen)");
  await expectNoViolations(editor, "Integration form with data categories");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(editor).toHaveURL(new RegExp(`/integrations/${id}$`));
  await expect(editor.getByTestId("integration-categories")).toContainText("Personal data");
  await expect(editor.getByTestId("integration-categories")).toContainText("Financial");
  await expect(editor.getByTestId("integration-categories").locator("[data-personal]")).toHaveText("●Contains personal data");
  // Personal data to Northwind Traders, which has no DPA: the flow is risky now.
  await editor.goto("/data-flows?risk=no_dpa");
  await expect(flowRows(editor).locator("th[scope=row] a")).toHaveText(["Payroll export", "Marketing feed"]);
  await editor.context().close();

  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto(`/integrations/${id}`);
  await expect(viewer.getByTestId("integration-categories")).toContainText("Personal data");
  await expect(viewer.getByTestId("integration-categories")).toContainText("Contains personal data");
  await expect(viewer.getByRole("link", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await expectNoViolations(viewer, "Integration with data categories (Viewer)");
  await viewer.goto(`/integrations/${id}/edit`);
  await expect(viewer.getByRole("heading", { level: 1, name: "You can't change integrations" })).toBeVisible();
  await viewer.context().close();
});

test("Data quality lists integrations and the two data-flow rules", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, ADMIN);
  await page.goto("/data-quality");
  await page.getByLabel("Kind").selectOption("integration");
  await expect(page).toHaveURL(/kind=integration/);
  const bare = page.getByRole("row").filter({ hasText: "Bare link" });
  await expect(bare).toContainText("Integration");
  await expect(bare.locator("[data-gap]")).toContainText(["Data flow not classified"]);
  await expect(bare.getByRole("link", { name: "Bare link" })).toHaveAttribute("href", /\/integrations\//);
  await expectNoViolations(page, "Data quality, integrations");
  await page.getByRole("tab", { name: "Checks" }).click();
  const rules = page.getByRole("region", { name: "Data-quality checks table" });
  await expect(rules.locator('[data-rule="flow_classification"]')).toContainText("Integrations");
  await expect(rules.locator('[data-rule="flow_categories"]')).toContainText("No data categories");
  await expect(rules.locator('[data-rule="eol_cycle_unknown"]')).toBeVisible();
  await page.context().close();
});

// ---- the installation's state and the capabilities ---------------------------------------------------------------------

test("The calendar says when endoflife.date data couldn't be read, and keeps one Past group", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, VIEWER);
  // As the API answers when a product's download failed, with an end-of-life row with no date sorting last.
  type Row = { id: string; record: { name: string }; eol_date: string | null; days_remaining: number | null };
  await page.route(/\/api\/v1\/end-of-life(\?.*)?$/, async (route) => {
    const body = await (await route.fetch()).json();
    const results = (body.results as Row[]).map((r) => (r.record.name === "Mystery App"
      ? { ...r, unknown_reason: "download_failed", why: "endoflife.date data for postgresql couldn't be read: HTTP 503" } : r));
    const ledger = results.find((r) => r.record.name === "Ledger DB")!;
    results.push({ ...ledger, id: "00000000-0000-4000-8000-0000000000aa", record: { ...ledger.record, name: "Dateless DB" }, eol_date: null, days_remaining: null });
    await route.fulfill({ json: { ...body, results } });
  });
  await page.goto("/end-of-life");
  const mystery = calendarRows(page).filter({ hasText: "Mystery App" });
  await expect(mystery.locator("[data-eol-status]")).toHaveText("⊘endoflife.date data couldn't be read");
  await expect(mystery.locator("[data-gap]")).toHaveCount(0);
  const timeline = page.getByRole("navigation", { name: "Timeline" });
  await expect(timeline.getByRole("link", { name: /Past end of life/ })).toHaveCount(1);
  await expect(timeline.getByRole("link").first()).toHaveText(/Past end of life\s*2/);
  await expect(page.locator('[id="eol-past"]')).toHaveCount(1);
  await expect(calendarRows(page).locator("th[scope=row] a")).toHaveText(["Ledger DB", "Dateless DB", "Payments API", "Intranet", "Build Server", "Mystery App"]);
  await expectNoViolations(page, "End-of-life calendar with a failed download");
  await page.context().close();
});

test("Without the eol and data_flows capabilities the pages, links and panel are gone", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, EDITOR);
  await page.route("**/api/v1/auth/me", async (route) => {
    const body = await (await route.fetch()).json();
    await route.fulfill({ json: { ...body, capabilities: (body.capabilities as string[]).filter((c) => c !== "eol" && c !== "data_flows") } });
  });
  const id = await recordId(page, "applications", "Ledger DB");
  await page.goto(`/applications/${id}`);
  await expect(page.getByRole("heading", { level: 1, name: "Ledger DB" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Details" })).toBeVisible();
  await expect(page.getByRole("region", { name: "End of life" })).toHaveCount(0);
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByRole("link", { name: "Integrations" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "End of life" })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Data flows" })).toHaveCount(0);
  await page.getByRole("button", { name: "Search or jump to…" }).click();
  const palette = page.getByRole("dialog", { name: "Command palette" });
  const input = palette.getByRole("combobox", { name: "Search records, pages and actions" });
  await input.fill("Integrations");
  await expect(palette.getByRole("option", { name: /^Integrations/ }).first()).toBeVisible();
  for (const name of ["Data flows", "End of life"]) {
    await input.fill(name);
    await expect(palette.getByRole("option", { name: /^Search everything/ })).toBeVisible();
    await expect(palette.getByRole("option", { name: new RegExp(`^${name}`) })).toHaveCount(0);
  }
  await page.keyboard.press("Escape");
  await page.goto("/end-of-life");
  await expect(page.getByTestId("eol-unavailable")).toContainText("End-of-life tracking isn't available");
  await page.goto("/data-flows");
  await expect(page.getByTestId("flows-unavailable")).toContainText("The data-flow map isn't available");
  await page.context().close();
});
