import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post } from "./helpers";

/** The vendor risk file in the SPA. The vendor page's Risk tab read only by a Viewer; an Editor editing the
 *  profile (a 400 shown on its field, the DPA gap closing), adding a SOC 2 report (the API's date error first, then the
 *  gap closing), a later SOC 2 report replacing it ("Replaced", dimmed, hidden with a toggle in the URL), a stale edit
 *  (412) and a delete behind a checkbox; the overview's and the register's filters in the URL with CSV export; the
 *  Needs attention "Vendor risk" queue and the vendor's reminders linking to the vendor page; axe in both themes.
 *
 *  The risk gaps are a Needs attention queue counted in the badge, so this file runs in its own organization (the
 *  Playwright-only `e2e/spec-org` route, `vendor_risk`), which puts seed_demo's vendor risk scenario back before each
 *  test: Workday Inc. critical with an expiring SOC 2 Type II report and a DPA, Zoom high risk with personal data, an
 *  expired SOC 2 report and no DPA (both gaps), Zylker Software low risk, Northwind Traders not assessed. */

const ADMIN = "vr-admin@example.test";
const EDITOR = "vr-editor@example.test";
const VIEWER = "vr-viewer@example.test";

// The tests run in order in one worker; each starts from the scenario (beforeEach), so one failing doesn't skip the rest.
async function reset(browser: Browser, baseURL: string | undefined) {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "e2e/spec-org", { which: "vendor_risk" });
  await contoso.context().close();
}
test.beforeEach(async ({ browser, baseURL }) => reset(browser, baseURL));

/** YYYY-MM-DD `days` from today (UTC, the spec organisation's timezone). */
const day = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
async function vendorId(page: Page, name: string): Promise<string> {
  const vendors: { id: string; name: string }[] = await (await page.request.get("/api/v1/vendors")).json();
  return vendors.find((v) => v.name === name)!.id;
}
const said = (page: Page, text: string) => expect(page.getByRole("status").filter({ hasText: text })).toHaveCount(1);
const gaps = (page: Page) => page.getByTestId("risk-gaps");
const docRow = (page: Page, title: string) => page.getByRole("region", { name: "Risk documents table" }).getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(title) }) });

test("A Viewer reads a vendor's risk file and can't change it", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, VIEWER);
  // From the Vendors tab, the vendor's name opens its page.
  await page.goto("/admin/people?tab=vendors");
  await page.getByRole("link", { name: "Workday Inc." }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Workday Inc." })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  const glance = page.getByRole("region", { name: "Risk at a glance" });
  await expect(glance).toContainText("Critical risk");
  await expect(glance).toContainText("No risk gaps");
  await expectNoViolations(page, "Vendor page, overview (Viewer)");

  await page.getByRole("tab", { name: "Risk" }).click();
  await expect(page).toHaveURL(/\?tab=risk$/);
  const risk = page.getByTestId("vendor-risk");
  await expect(gaps(page)).toContainText("No risk gaps.");
  await expect(page.getByTestId("risk-read-only")).toHaveText("You can read the risk file. Editors and Admins can change it.");
  const profile = risk.getByRole("region", { name: "Risk profile" });
  await expect(profile).toContainText("Critical risk");
  await expect(profile).toContainText("SOC 2 Type II");
  await expect(profile.locator("[data-status]")).toHaveText(["◐Expiring"]);
  await expect(docRow(page, "Insurance certificate")).toContainText("Expiring");
  await expect(docRow(page, "Penetration test report")).toContainText("Current");
  // Nothing to change for a Viewer.
  await expect(page.getByRole("button", { name: "Edit risk profile" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add document" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Edit / })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Delete / })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Edit vendor" })).toHaveCount(0);
  await expectNoViolations(page, "Vendor page, Risk tab (Viewer)");
  await page.context().close();
});

test("An Editor fills the risk file: the profile's DPA, a SOC 2 report after its date error, a later report replacing it", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, EDITOR);
  const zoom = await vendorId(page, "Zoom");
  await page.goto(`/vendors/${zoom}?tab=risk`);
  await expect(page.getByRole("heading", { level: 1, name: "Zoom" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Risk (2 gaps)" })).toHaveAttribute("aria-selected", "true");
  await expect(gaps(page).locator("[data-gap]")).toHaveText(["⚠No current SOC 2 or equivalent", "⚠No signed DPA"]);
  await expect(docRow(page, "SOC 2 report")).toContainText("Expired");

  // The profile is edited in place: focus moves into the form, and back to the button that opened it.
  const editProfile = page.getByRole("button", { name: "Edit risk profile" });
  await editProfile.click();
  const form = page.getByRole("form", { name: "Edit the risk profile of Zoom" });
  await expect(form.getByLabel("Risk tier")).toBeFocused();
  await form.getByRole("button", { name: "Cancel" }).click();
  await expect(form).toHaveCount(0);
  await expect(editProfile).toBeFocused();
  await editProfile.click();
  await form.getByLabel("DPA", { exact: true }).selectOption("yes");
  await form.getByLabel("Signed on").fill(day(-10));
  await expectNoViolations(page, "Risk profile form");
  // Fields the API refuses: one the form shows (on its block), and one it has no field for (named in the summary).
  await page.route("**/api/v1/vendors/*/risk", (route) => (route.request().method() === "PATCH"
    ? route.fulfill({ status: 400, contentType: "application/problem+json", body: JSON.stringify({ type: "/problems/validation", title: "Invalid", status: 400, detail: "Check the fields.",
      errors: { assurance_expires_on: ["Worked out from the issue date and the validity; send those instead."], foo: ["This field isn't accepted here."] } }) })
    : route.fallback()));
  await form.getByRole("button", { name: "Save risk profile" }).click();
  await expect(form.getByRole("group", { name: "Expires on (worked out):" })).toContainText("Worked out from the issue date and the validity");
  await expect(form.getByRole("alert")).toContainText("foo: This field isn't accepted here.");
  await expect(form.getByRole("group", { name: "Expires on (worked out):" })).toBeFocused();
  await expectNoViolations(page, "Risk profile form with the API's field errors");
  await page.unroute("**/api/v1/vendors/*/risk");
  // Saved for real: the DPA gap closes, and focus is back on the button.
  await form.getByRole("button", { name: "Save risk profile" }).click();
  await said(page, "Risk file of Zoom saved.");
  await expect(editProfile).toBeFocused();
  await expect(gaps(page).locator("[data-gap]")).toHaveText(["⚠No current SOC 2 or equivalent"]);
  await expect(page.getByRole("region", { name: "Risk profile" })).toContainText(`Signed ${day(-10)}`);

  // A SOC 2 report needs a date: the API's error shows on the expiry field first.
  await page.getByRole("button", { name: "Add document" }).click();
  const add = page.getByRole("dialog", { name: "Add a risk document to Zoom" });
  await add.getByLabel("Kind (required)").selectOption("soc2");
  await add.getByLabel("Title").fill("FY26");
  await add.getByRole("button", { name: "Add document" }).click();
  await expect(add.locator("#doc-expires_on-error")).toHaveText("A SOC 2 report or ISO 27001 certificate needs an expiry date, or an issue date and a validity in months.");
  await expect(add.getByLabel("Expires on (required)")).toBeFocused();
  await expectNoViolations(page, "Add document dialog with the API's date error");
  await add.getByLabel("Expires on (required)").fill(day(200));
  await add.getByRole("button", { name: "Add document" }).click();
  await expect(add).toBeHidden();
  await said(page, "SOC 2 report (FY26) added.");
  // The assurance gap closes, and the expired report is replaced by the new one.
  await expect(gaps(page)).toContainText("No risk gaps.");
  await expect(page.getByRole("tab", { name: "Risk", exact: true })).toBeVisible();
  const old = docRow(page, "SOC 2 Type II");
  await expect(old).toHaveAttribute("data-superseded", "");
  await expect(old).toContainText("Replaced");
  await expect(docRow(page, "FY26")).not.toContainText("Replaced");

  // A later SOC 2 report, its expiry worked out from a validity, replaces FY26 in turn.
  await page.getByRole("button", { name: "Add document" }).click();
  await add.getByLabel("Kind (required)").selectOption("soc2");
  await add.getByLabel("Title").fill("FY27");
  await add.getByRole("radio", { name: "Work it out from a validity in months" }).check();
  await expect(add.getByLabel("Expires on (required)")).toHaveCount(0);
  await add.getByLabel("Issued on (required)").fill(day(0));
  await add.getByLabel("Valid for (months)").fill("12");
  await add.getByRole("button", { name: "Add document" }).click();
  // The saved note is outside the dialog, which hides the rest of the page from assistive technology (and so from
  // getByRole) while it is open: wait for the save to close the dialog first, as the FY26 step does.
  await expect(add).toBeHidden();
  await said(page, "SOC 2 report (FY27) added.");
  await expect(docRow(page, "FY26")).toContainText("Replaced");
  await expect(docRow(page, "FY27")).toContainText("12 months from issue");
  await expect(docRow(page, "FY27")).not.toContainText("Replaced");
  await expectNoViolations(page, "Risk tab with replaced documents");

  // Hide replaced documents: kept in the URL.
  await page.getByLabel(/Hide replaced documents/).check();
  await expect(page).toHaveURL(/replaced=hide/);
  await expect(docRow(page, "FY26")).toHaveCount(0);
  await expect(docRow(page, "FY27")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel(/Hide replaced documents/)).toBeChecked();
  await expect(docRow(page, "FY26")).toHaveCount(0);
  await page.getByLabel(/Hide replaced documents/).uncheck();

  // A stale edit: someone else changed the document after the dialog read it (412), then the latest version.
  await page.getByRole("button", { name: "Edit SOC 2 report (FY27)" }).click();
  const edit = page.getByRole("dialog", { name: "Edit SOC 2 report (FY27)" });
  await expect(edit.getByLabel("Title")).toHaveValue("FY27");
  const fy27 = (await (await page.request.get(`/api/v1/vendors/${zoom}/risk-documents`)).json() as { id: string; title: string; expires_on: string }[]).find((d) => d.title === "FY27")!;
  // Switching a worked-out expiry to an entered one starts from the date it gave.
  await edit.getByRole("radio", { name: "Enter the expiry date" }).check();
  await expect(edit.getByLabel("Expires on (required)")).toHaveValue(fy27.expires_on);
  await edit.getByRole("radio", { name: "Work it out from a validity in months" }).check();
  await expect(edit.getByLabel("Valid for (months)")).toHaveValue("12");
  await post(page, `vendor-risk-documents/${fy27.id}`, { notes: "Changed elsewhere" }, "PATCH");
  await edit.getByLabel("Title").fill("FY27 audited");
  await edit.getByRole("button", { name: "Save document" }).click();
  await expect(edit.getByRole("alert").filter({ hasText: "Someone else saved changes to this document" })).toBeVisible();
  await edit.getByRole("button", { name: "Load the latest version" }).click();
  await expect(edit.getByLabel("Notes")).toHaveValue("Changed elsewhere");
  await edit.getByLabel("Title").fill("FY27 audited");
  await edit.getByRole("button", { name: "Save document" }).click();
  await expect(edit).toBeHidden();
  await said(page, "SOC 2 report (FY27 audited) saved.");

  // Delete asks for a checkbox; FY26 is in force again once FY27 goes.
  await page.getByRole("button", { name: "Delete SOC 2 report (FY27 audited)" }).click();
  const del = page.getByRole("dialog", { name: "Delete SOC 2 report (FY27 audited)?" });
  await del.getByRole("button", { name: "Delete SOC 2 report (FY27 audited)" }).click();
  await expect(del.getByRole("alert")).toHaveText("Tick the box to confirm the document is removed.");
  await expectNoViolations(page, "Delete document dialog");
  await del.getByLabel("I understand this document is removed from the vendor's risk file.").check();
  await del.getByRole("button", { name: "Delete SOC 2 report (FY27 audited)" }).click();
  await expect(del).toBeHidden();
  await said(page, "SOC 2 report (FY27 audited) deleted.");
  await expect(docRow(page, "FY27")).toHaveCount(0);
  await expect(docRow(page, "FY26")).not.toContainText("Replaced");
  await page.context().close();
});

test("The overview's and the register's filters live in the URL, with CSV export", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, VIEWER);
  await page.goto("/");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Vendor risk" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Vendor risk" })).toBeVisible();
  const table = page.getByRole("region", { name: "Vendor risk table" });
  // The riskiest first: critical, high, low, then not assessed.
  await expect(table.getByRole("rowheader")).toHaveText([/^Workday Inc\./, /^Zoom/, /^Zylker Software/, /^Northwind Traders/]);
  await expect(table.getByRole("row").filter({ hasText: "Zoom" })).toContainText("No current SOC 2 or equivalent");
  await expectNoViolations(page, "Vendor risk overview");

  await page.getByLabel("Gap").selectOption("dpa_missing");
  await expect(page).toHaveURL(/gap=dpa_missing/);
  await expect(table.getByRole("rowheader")).toHaveText([/^Zoom/]);
  await page.reload();
  await expect(page.getByLabel("Gap")).toHaveValue("dpa_missing");
  await expect(table.getByRole("rowheader")).toHaveText([/^Zoom/]);
  await page.getByLabel("Risk tier").selectOption("critical");
  await expect(page).toHaveURL(/tier=critical/);
  await expect(page.getByText("No vendors match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/vendor-risk$/);

  await page.getByLabel("Expiring within (days)").fill("30");
  await page.getByLabel("Expiring within (days)").press("Enter");
  await expect(page).toHaveURL(/expiring_within=30/);
  await expect(table.getByRole("rowheader")).toHaveText([/^Workday Inc\./]);
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await page.getByLabel("Expired documents").selectOption("true");
  await expect(page).toHaveURL(/expired=true/);
  await expect(table.getByRole("rowheader")).toHaveText([/^Zoom/]);

  // The export takes the same filters, and says where it went.
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/^softwarden-vendor-risk-\d{4}-\d{2}-\d{2}\.csv$/);
  await said(page, "Exported the vendor risk overview to");

  // The register: replaced documents hidden unless asked for.
  await page.getByRole("navigation", { name: "Vendor risk sections" }).getByRole("link", { name: "Documents" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Vendor risk documents" })).toBeVisible();
  const docs = page.getByRole("region", { name: "Risk documents table" });
  await expect(page.getByLabel("Replaced documents")).toHaveValue("");
  await page.getByLabel("Kind").selectOption("soc2");
  await expect(page).toHaveURL(/kind=soc2/);
  await expect(docs.getByRole("row").filter({ hasText: "Zoom" })).toContainText("Expired");
  await page.getByLabel("Status").selectOption("expiring");
  await expect(page).toHaveURL(/status=expiring/);
  await expect(page.getByText("No documents match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await page.getByLabel("Replaced documents").selectOption("all");
  await expect(page).toHaveURL(/superseded=all/);
  // Sorted by expiry; the header says so and turns it around.
  const expires = docs.getByRole("columnheader", { name: /Expires/ });
  await expect(expires).toHaveAttribute("aria-sort", "ascending");
  await expect(docs.getByRole("rowheader").first()).toContainText("SOC 2 report");
  await expires.getByRole("button").click();
  await expect(page).toHaveURL(/ordering=-expiry/);
  await expect(expires).toHaveAttribute("aria-sort", "descending");
  await expectNoViolations(page, "Vendor risk documents register");
  const csv = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  expect((await csv).suggestedFilename()).toMatch(/^softwarden-vendor-risk-documents-\d{4}-\d{2}-\d{2}\.csv$/);
  await page.context().close();
});

test("Needs attention lists vendor risk gaps for the coordinator, and vendor reminders open the vendor", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, ADMIN);
  const zoom = await vendorId(page, "Zoom");
  const workday = await vendorId(page, "Workday Inc.");
  await page.goto("/#h-vendor-risk");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  // Counted with the others: the coordinator's "Assigned to me" includes it.
  await expect(page.getByRole("navigation", { name: "Queues" }).getByRole("link", { name: /Vendor risk/ })).toContainText("1");
  const queue = page.getByRole("region", { name: "Queue table: Vendor risk" });
  await expect(page.locator("#h-vendor-risk")).toBeFocused();
  const row = queue.getByRole("row").filter({ hasText: "Zoom" });
  await expect(row).toContainText("High risk");
  await expect(row.locator("[data-gap]")).toHaveText(["⚠No current SOC 2 or equivalent", "⚠No signed DPA"]);
  await expect(row).toContainText("Vera Admin");
  await expectNoViolations(page, "Needs attention with the Vendor risk queue");

  // Reminders of the vendor's risk documents open the vendor page.
  const reminders = page.getByRole("region", { name: "Queue table: Reminders" });
  const insurance = reminders.getByRole("row").filter({ hasText: "Insurance certificate (Cyber liability) expires" });
  await expect(insurance.getByRole("link", { name: "Workday Inc.", exact: true })).toHaveAttribute("href", `/vendors/${workday}`);

  await row.getByRole("link", { name: "Open risk file of Zoom" }).click();
  await expect(page).toHaveURL(new RegExp(`/vendors/${zoom}\\?tab=risk$`));
  await expect(page.getByRole("tab", { name: /^Risk/ })).toHaveAttribute("aria-selected", "true");
  await expect(gaps(page).locator("[data-gap]")).toHaveCount(2);

  await page.goto("/");
  await page.getByRole("region", { name: "Queue table: Reminders" }).getByRole("link", { name: "Workday Inc.", exact: true }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Workday Inc." })).toBeVisible();
  await page.context().close();

  // A Viewer sees the row with a way to read the file.
  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto("/?scope=all");
  await expect(viewer.getByRole("region", { name: "Queue table: Vendor risk" }).getByRole("link", { name: "View risk file of Zoom" })).toBeVisible();
  await viewer.context().close();
});

test("Starting a risk file someone else started meanwhile: 412, the latest version, then the save", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, EDITOR);
  const northwind = await vendorId(page, "Northwind Traders");
  await page.goto(`/vendors/${northwind}?tab=risk`);
  const panel = page.getByRole("region", { name: "Risk profile" });
  await expect(panel).toContainText("Not started yet");
  // The form holds the ETag of a vendor with no risk file yet.
  await page.getByRole("button", { name: "Edit risk profile" }).click();
  const form = page.getByRole("form", { name: "Edit the risk profile of Northwind Traders" });
  await expect(form.getByLabel("Risk tier")).toHaveValue("");
  // Someone else starts it.
  await post(page, `vendors/${northwind}/risk`, { risk_tier: "medium", notes: "Started elsewhere" }, "PATCH");
  await form.getByLabel("Risk tier").selectOption("high");
  await form.getByRole("button", { name: "Save risk profile" }).click();
  const stale = panel.getByRole("alert").filter({ hasText: "Someone else saved changes to this risk file" });
  await expect(stale).toBeVisible();
  await expectNoViolations(page, "Risk profile form after a 412");
  await stale.getByRole("button", { name: "Load the latest version" }).click();
  await expect(stale).toHaveCount(0);
  // The form starts again from their values.
  await expect(form.getByLabel("Risk tier")).toHaveValue("medium");
  await expect(form.getByLabel("Notes")).toHaveValue("Started elsewhere");
  await expect(form.getByLabel("Risk tier")).toBeFocused();
  await form.getByLabel("Risk tier").selectOption("high");
  await form.getByRole("button", { name: "Save risk profile" }).click();
  await said(page, "Risk file of Northwind Traders saved.");
  await expect(panel).toContainText("High risk");
  await expect(panel).toContainText("Started elsewhere");
  await page.context().close();
});

test("A vendor risk row can be snoozed; it stays listed, marked", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, ADMIN);
  await page.goto("/#h-vendor-risk");
  const row = page.getByRole("region", { name: "Queue table: Vendor risk" }).getByRole("row").filter({ hasText: "Zoom" });
  await row.getByRole("button", { name: "Notifications for Zoom" }).click();
  await page.getByRole("menuitem", { name: "Snooze notifications…" }).click();
  const dialog = page.getByRole("dialog", { name: "Snooze notifications for Zoom?" });
  await dialog.getByLabel("Snooze until").fill(day(5));
  await expectNoViolations(page, "Snooze dialog for a vendor risk row");
  await dialog.getByRole("button", { name: "Snooze" }).click();
  await said(page, `snoozed until ${day(5)}`);
  await expect(row).toContainText(`Snoozed until ${day(5)}`);
  await expect(row.locator("[data-gap]")).toHaveCount(2);
  await row.getByRole("button", { name: "Notifications for Zoom" }).click();
  await page.getByRole("menuitem", { name: "End snooze" }).click();
  await said(page, "Notifications for Zoom are back on.");
  await expect(row).not.toContainText("Snoozed until");
  await page.context().close();
});
