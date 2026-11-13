import { expect, test, type Browser, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, personId, post, token } from "./helpers";

/** Bulk edit extended to the IT steward, department, lifecycle, criticality and custom fields (with
 *  request-level custom field errors on the input, refused rows with their field errors, and a retirement refused for
 *  what depends on the record), and bulk delete (Admins) with blocked records, Deselect blocked and a 409 race.
 *
 *  Custom fields are organization-wide, so that test runs in the enterprise organization (`e2e/enterprise-org`
 *  with `which: "secondary"`; calling the route turns that organization's compliance profile off, so these specs never
 *  use the one compliance.spec.ts switches on), and deletes its fields at the end. */

// axe runs in the light and the dark theme at every check (e2e/axe.ts).
test.describe.configure({ mode: "serial", timeout: 180_000 });

const said = (page: Page, text: string | RegExp) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeAttached();

type Rec = { id: string; name: string };
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** A preview line, "Lifecycle: Active → Deprecated" (the arrow is hidden and "changes to" is said instead). */
const change = (field: string, before: string, after: string) => new RegExp(`${esc(field)}: ${esc(before)}\\s*→\\s*changes to ${esc(after)}`);

async function openBulk(page: Page, names: string[], action: "Bulk edit…" | "Bulk delete…") {
  for (const n of names) await page.getByRole("checkbox", { name: `Select ${n}` }).check();
  await page.getByRole("button", { name: action }).click();
}

test("Bulk edit: lifecycle, criticality, IT steward and department, previewed before and after with confirmation counts", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const dana = await personId(page, "Dana Ortiz");
  const department: Rec = await post(page, "departments", { name: `${t} Finance` });
  const stewards: { id: number; name: string }[] = await (await page.request.get("/api/v1/stewards")).json();
  const ids: string[] = [];
  for (const n of ["A", "B"]) {
    const app: Rec = await post(page, "applications", { name: `${t} ${n}`, lifecycle: "active", criticality: "high", business_owner: dana });
    await post(page, `applications/${app.id}/reviews`, { confirmed_by: dana, evidence: "Synthetic" });
    ids.push(app.id);
  }
  // A new record's IT steward is whoever made it; move them to someone else.
  const current: { it_steward: { id: number; name: string } } = await (await page.request.get(`/api/v1/applications/${ids[0]}`)).json();
  const steward = stewards.find((u) => u.id !== current.it_steward.id)!;

  await page.goto(`/applications?q=${t}`);
  await expect(page.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} B` })).toBeVisible();
  await expect(page.getByRole("button", { name: "Bulk delete…" })).toHaveCount(0); // Editors don't delete
  await openBulk(page, [`${t} A`, `${t} B`], "Bulk edit…");
  await expect(page.getByRole("button", { name: "Bulk delete…" })).toHaveCount(0);
  const dialog = page.getByRole("dialog", { name: "Bulk edit 2 applications" });
  await dialog.getByLabel("Lifecycle", { exact: true }).selectOption("deprecated");
  await dialog.getByLabel("Criticality", { exact: true }).selectOption({ label: "Clear criticality (shows as a gap)" });
  await dialog.getByLabel("IT steward", { exact: true }).selectOption(String(steward.id));
  await dialog.getByLabel("Department", { exact: true }).selectOption({ label: `${t} Finance` });
  await expectNoViolations(page, "Bulk edit dialog with the extra fields");
  await dialog.getByRole("button", { name: "Preview changes" }).click();

  await expect(dialog.getByRole("status").filter({ hasText: "will change" })).toContainText("2 records will change; 2 will need confirmation; 0 unchanged.");
  const preview = dialog.getByRole("region", { name: "Bulk edit preview table" });
  for (const n of ["A", "B"]) {
    const row = preview.getByRole("row", { name: new RegExp(`${t} ${n}`) });
    await expect(row).toContainText(change("Lifecycle", "Active", "Deprecated"));
    await expect(row).toContainText(change("Criticality", "High", "not set"));
    await expect(row).toContainText(change("IT steward", current.it_steward.name, steward.name));
    await expect(row).toContainText(change("Department", "not set", `${t} Finance`));
    await expect(row).toContainText("Will need confirmation");
  }
  await expectNoViolations(page, "Bulk edit preview with the extra fields");
  await dialog.getByRole("button", { name: "Apply to 2 records" }).click();
  await expect(dialog).toHaveCount(0);
  await said(page, "Updated 2 records; 0 unchanged. 2 records are now Needs confirmation.");
  for (const id of ids) {
    const rec = await (await page.request.get(`/api/v1/applications/${id}`)).json();
    expect(rec.lifecycle).toBe("deprecated");
    expect(rec.criticality).toBeNull();
    expect(rec.it_steward.id).toBe(steward.id);
    expect(rec.department.id).toBe(department.id);
  }
});

test("Bulk edit: retiring a record active records depend on is refused with how to go on; selecting the dependent too retires both", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const portal: Rec = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const sync: Rec = await post(page, "applications", { name: `${t} Sync`, lifecycle: "active" });
  await post(page, "relationships", { type: "depends_on", source: portal.id, target: sync.id });

  await page.goto(`/applications?q=${t}`);
  await expect(page.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Sync` })).toBeVisible();
  await openBulk(page, [`${t} Sync`], "Bulk edit…");
  const dialog = page.getByRole("dialog", { name: "Bulk edit 1 application" });
  await dialog.getByLabel("Lifecycle", { exact: true }).selectOption("retired");
  await dialog.getByRole("button", { name: "Preview changes" }).click();
  const row = dialog.getByRole("region", { name: "Bulk edit preview table" }).getByRole("row", { name: new RegExp(`${t} Sync`) });
  await expect(row).toContainText(`Still depended on by 1 active record: ${t} Portal.`);
  await expect(row).toContainText("Can't change");
  const help = row.getByTestId("retire-help");
  await expect(help).toContainText("Retire it on its own page, or select its dependents too.");
  await expect(help.getByRole("link", { name: `Retire ${t} Sync on its own page` })).toHaveAttribute("href", `/applications/${sync.id}`);
  await expect(help.getByRole("link", { name: `What depends on ${t} Sync` })).toHaveAttribute("href", `/applications/${sync.id}?tab=map&dependents=${sync.id}`);
  await expect(dialog.getByRole("button", { name: "Fix the errors to apply" })).toBeDisabled();
  await expectNoViolations(page, "Bulk edit preview with a refused retirement");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // With the dependent selected too, both retire (the dependent first).
  await openBulk(page, [`${t} Portal`], "Bulk edit…");
  const both = page.getByRole("dialog", { name: "Bulk edit 2 applications" });
  await both.getByLabel("Lifecycle", { exact: true }).selectOption("retired");
  await both.getByRole("button", { name: "Preview changes" }).click();
  await expect(both.getByRole("status").filter({ hasText: "will change" })).toContainText("2 records will change");
  await both.getByRole("button", { name: "Apply to 2 records" }).click();
  await said(page, "Updated 2 records");
  for (const r of [portal, sync]) expect((await (await page.request.get(`/api/v1/applications/${r.id}`)).json()).lifecycle).toBe("retired");
});

test("Bulk edit: a refused apply shows each row's field errors (intercepted)", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const a: Rec = await post(page, "applications", { name: `${t} One`, lifecycle: "active" });
  await post(page, "applications", { name: `${t} Two`, lifecycle: "active" });
  await page.goto(`/applications?q=${t}`);
  await expect(page.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Two` })).toBeVisible();
  await openBulk(page, [`${t} One`, `${t} Two`], "Bulk edit…");
  const dialog = page.getByRole("dialog", { name: "Bulk edit 2 applications" });
  await dialog.getByLabel("Criticality", { exact: true }).selectOption("low");
  await dialog.getByRole("button", { name: "Preview changes" }).click();
  await expect(dialog.getByRole("button", { name: "Apply to 2 records" })).toBeEnabled();

  // Someone made the owner inactive between preview and apply: the record's own write refuses it.
  await page.route("**/api/v1/bulk-edit/apply", (route) => route.fulfill({
    status: 400, contentType: "application/problem+json",
    body: JSON.stringify({
      type: "about:blank", title: "Bad Request", status: 400, detail: "Some fields need attention.",
      errors: { ids: ["Some selected records can't take this change. Nothing was saved."] },
      rows: [{ id: a.id, name: `${t} One`, errors: [], field_errors: { criticality: ["Choose low, medium, high or critical."], "custom_field_data.tier": ["Choose one of: Gold, Silver."] } }],
    }),
  }));
  await dialog.getByRole("button", { name: "Apply to 2 records" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Nothing was changed: 1 record can't take this change.");
  const row = dialog.getByRole("row", { name: new RegExp(`${t} One`) });
  await expect(row).toContainText("Criticality: Choose low, medium, high or critical.");
  await expect(row).toContainText("tier: Choose one of: Gold, Silver.");
  await expect(row).toContainText("Can't change");
  await expect(dialog.getByRole("row", { name: new RegExp(`${t} Two`) })).toContainText("Will change");
  await expect(dialog.getByRole("button", { name: "Fix the errors to apply" })).toBeDisabled();
  await expectNoViolations(page, "Bulk edit with refused rows");
  await page.unroute("**/api/v1/bulk-edit/apply");
  expect((await (await page.request.get(`/api/v1/applications/${a.id}`)).json()).criticality).toBeNull();
});

/** The enterprise organization's Admin, signed in there (custom fields stay out of the shared organization). */
async function enterprise(browser: Browser, baseURL: string | undefined) {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const { email } = await post(contoso, "e2e/enterprise-org", { which: "secondary" });
  await contoso.context().close();
  return pageAs(browser, baseURL, email);
}

test("Bulk edit: a custom field is set and cleared; a bad value is shown on its input", async ({ browser, baseURL }) => {
  const t = token();
  const page = await enterprise(browser, baseURL);
  const suffix = t.toLowerCase();
  const tier = await post(page, "custom-fields", { name: `tier_${suffix}`, label: `Tier ${t}`, type: "select", choices: ["Gold", "Silver"], applies_to: ["component"] });
  const seats = await post(page, "custom-fields", { name: `seats_${suffix}`, label: `Seats ${t}`, type: "integer", applies_to: ["component"] });
  try {
    const ids: string[] = [];
    for (const n of ["Job", "Feed"]) ids.push((await post(page, "components", { name: `${t} ${n}`, lifecycle: "active", component_type: "script" })).id);
    await page.goto(`/applications?kind=components&q=${t}`);
    await expect(page.getByRole("region", { name: "Components table" }).getByRole("link", { name: `${t} Feed` })).toBeVisible();
    await openBulk(page, [`${t} Job`, `${t} Feed`], "Bulk edit…");
    const dialog = page.getByRole("dialog", { name: "Bulk edit 2 components" });
    await dialog.getByLabel("Change a custom field").selectOption({ label: `Tier ${t}` });
    await expect(dialog.getByLabel(`Tier ${t}`)).toBeFocused();
    await dialog.getByLabel(`Tier ${t}`).selectOption("Gold");
    await dialog.getByLabel("Change another custom field").selectOption({ label: `Seats ${t}` });
    await dialog.getByLabel(`Seats ${t}`).fill("many");
    await expectNoViolations(page, "Bulk edit with custom fields chosen");
    await dialog.getByRole("button", { name: "Preview changes" }).click();
    // A request-level 400 keyed custom_field_data.<name>: on the input and in the summary.
    const seatsInput = dialog.getByLabel(`Seats ${t}`);
    await expect(seatsInput).toHaveAttribute("aria-invalid", "true");
    await expect(seatsInput).toHaveAccessibleDescription(/whole number/i);
    await expect(dialog.getByRole("alert").filter({ hasText: "Fix this field" })).toContainText(/whole number/i);
    await expectNoViolations(page, "Bulk edit with a custom field error");
    await dialog.getByRole("button", { name: `Don't change Seats ${t}` }).click();
    await expect(dialog.getByLabel(`Seats ${t}`)).toHaveCount(0);
    await dialog.getByRole("button", { name: "Preview changes" }).click();
    for (const n of ["Job", "Feed"]) await expect(dialog.getByRole("row", { name: new RegExp(`${t} ${n}`) })).toContainText(change(`Tier ${t}`, "Not set", "Gold"));
    await dialog.getByRole("button", { name: "Apply to 2 records" }).click();
    await said(page, "Updated 2 records");
    for (const id of ids) expect((await (await page.request.get(`/api/v1/components/${id}`)).json()).custom_field_data[tier.name]).toBe("Gold");

    // Cleared: the field chosen, its value left Not set.
    await openBulk(page, [`${t} Job`, `${t} Feed`], "Bulk edit…");
    const again = page.getByRole("dialog", { name: "Bulk edit 2 components" });
    await again.getByLabel("Change a custom field").selectOption({ label: `Tier ${t}` });
    await expect(again.getByLabel(`Tier ${t}`)).toHaveValue("");
    await again.getByRole("button", { name: "Preview changes" }).click();
    await expect(again.getByRole("row", { name: new RegExp(`${t} Job`) })).toContainText(change(`Tier ${t}`, "Gold", "Not set"));
    await again.getByRole("button", { name: "Apply to 2 records" }).click();
    await said(page, "Updated 2 records");
    for (const id of ids) expect((await (await page.request.get(`/api/v1/components/${id}`)).json()).custom_field_data[tier.name]).toBeNull();
  } finally {
    for (const f of [tier, seats]) await post(page, `custom-fields/${f.id}`, undefined, "DELETE");
  }
});

test("Bulk delete: blocked and deletable rows, Deselect blocked, confirm, apply; Editors have no action", async ({ browser, baseURL }) => {
  const t = token();
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const keep: Rec = await post(admin, "applications", { name: `${t} Elsewhere`, lifecycle: "active" });
  const one: Rec = await post(admin, "applications", { name: `${t} Old one`, lifecycle: "retired" });
  const two: Rec = await post(admin, "applications", { name: `${t} Old two`, lifecycle: "retired" });
  const held: Rec = await post(admin, "applications", { name: `${t} Held`, lifecycle: "active" });
  await post(admin, `applications/${one.id}/attachments`, { url: "https://example.test/runbook", title: `${t} runbook` });
  await post(admin, `applications/${held.id}/instances`, { name: `${t} Production`, environment: "production" });
  await post(admin, "relationships", { type: "depends_on", source: keep.id, target: held.id });

  await admin.goto(`/applications?q=${t}&preset=all`);
  await expect(admin.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Held` })).toBeVisible();
  await openBulk(admin, [`${t} Old one`, `${t} Old two`, `${t} Held`], "Bulk delete…");
  const dialog = admin.getByRole("dialog", { name: "Delete 3 applications" });
  await expect(dialog.getByRole("status")).toHaveText("2 of 3 can be deleted; 1 is blocked.");
  const rows = dialog.getByRole("region", { name: "Bulk delete preview table" });
  await expect(rows.getByRole("row", { name: new RegExp(`${t} Held`) })).toContainText("Blocked");
  await expect(rows.getByRole("row", { name: new RegExp(`${t} Held`) })).toContainText("Referenced by 1 instance, 1 relationship. Archive it instead.");
  await expect(rows.getByRole("row", { name: new RegExp(`${t} Old one`) })).toContainText("Can be deleted");
  await expect(rows.getByRole("row", { name: new RegExp(`${t} Old one`) })).toContainText("Also deletes its 1 attachment.");
  await expect(rows.getByRole("row", { name: new RegExp(`${t} Old two`) })).toContainText("Nothing else is deleted with it.");
  await expect(dialog.getByRole("button", { name: /^Delete / })).toHaveCount(0); // nothing is offered while one is blocked
  await expectNoViolations(admin, "Bulk delete with a blocked record");

  await dialog.getByRole("button", { name: "Deselect blocked (1)" }).click();
  const narrowed = admin.getByRole("dialog", { name: "Delete 2 applications" });
  await expect(narrowed.getByRole("status")).toHaveText("All 2 applications can be deleted.");
  await expect(narrowed.getByRole("status")).toBeFocused();
  // The list behind the dialog (hidden from assistive tech while it is open) has Held deselected.
  await expect(admin.getByRole("checkbox", { name: `Select ${t} Held`, includeHidden: true })).not.toBeChecked();
  const remove = narrowed.getByRole("button", { name: "Delete 2 applications" });
  await remove.click();
  await expect(narrowed.getByLabel(/Permanently delete 2 applications, with their 1 attachment/)).toBeFocused();
  await expect(narrowed).toContainText("Tick the box to confirm that 2 applications are deleted.");
  await expectNoViolations(admin, "Bulk delete ready to confirm, with the confirmation error");
  await narrowed.getByLabel(/Permanently delete 2 applications/).check();
  await remove.click();
  await expect(narrowed).toHaveCount(0);
  await said(admin, "Deleted 2 applications. Their 1 attachment went with them.");
  await expect(admin.getByRole("heading", { level: 1, name: "Applications" })).toBeFocused();
  await expect(admin.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Old one` })).toHaveCount(0);
  await expect(admin.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Held` })).toBeVisible();
  for (const r of [one, two]) expect((await admin.request.get(`/api/v1/applications/${r.id}`)).status()).toBe(404);

  // An Editor selects rows but gets no Bulk delete.
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await editor.goto(`/applications?q=${t}&preset=all`);
  await editor.getByRole("checkbox", { name: `Select ${t} Held` }).check();
  await expect(editor.getByRole("button", { name: "Bulk edit…" })).toBeVisible();
  await expect(editor.getByRole("button", { name: "Bulk delete…" })).toHaveCount(0);
});

test("Bulk delete: a record referenced after the preview (409) shows as blocked and nothing is deleted", async ({ browser, baseURL }) => {
  const t = token();
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const gone: Rec = await post(admin, "applications", { name: `${t} Racing`, lifecycle: "retired" });
  await admin.goto(`/applications?q=${t}&preset=all`);
  await expect(admin.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Racing` })).toBeVisible();
  await openBulk(admin, [`${t} Racing`], "Bulk delete…");
  const dialog = admin.getByRole("dialog", { name: "Delete 1 application" });
  await expect(dialog.getByRole("status")).toHaveText("The selected application can be deleted.");
  await admin.route("**/api/v1/bulk-delete/apply", (route) => route.fulfill({
    status: 409, contentType: "application/problem+json",
    body: JSON.stringify({
      type: "about:blank", title: "Conflict", status: 409,
      detail: "1 of the selected records can't be deleted because other records reference them. Nothing was deleted.",
      rows: [{ id: gone.id, name: `${t} Racing`, blocking_references: { relationship: 1 }, errors: ["It has 1 relationship, so it can't be deleted. Archive it instead."] }],
    }),
  }));
  await dialog.getByLabel(/Permanently delete 1 application/).check();
  await dialog.getByRole("button", { name: "Delete 1 application" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Nothing was deleted.");
  await expect(dialog.getByRole("alert")).toContainText("Other records started referencing it after the check");
  await expect(dialog.getByRole("status")).toHaveText("0 of 1 can be deleted; 1 is blocked.");
  await expect(dialog.getByRole("row", { name: new RegExp(`${t} Racing`) })).toContainText("Referenced by 1 relationship.");
  await expect(dialog.getByRole("button", { name: /^Delete / })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: /Deselect blocked/ })).toHaveCount(0);
  await expectNoViolations(admin, "Bulk delete after a 409");
  await admin.unroute("**/api/v1/bulk-delete/apply");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(admin.getByRole("button", { name: "Bulk delete…" })).toBeFocused();
  expect((await admin.request.get(`/api/v1/applications/${gone.id}`)).status()).toBe(200);
});

test("Bulk dialogs: if their code can't load, the message is beside the bulk bar, not below the table", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await post(page, "applications", { name: `${t} Offline`, lifecycle: "active" });
  await page.route("**/src/features/catalog/bulk.tsx*", (route) => route.abort());
  await page.goto(`/applications?q=${t}`);
  await expect(page.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Offline` })).toBeVisible();
  await openBulk(page, [`${t} Offline`], "Bulk edit…");
  const alert = page.getByRole("alert").filter({ hasText: "Couldn't load this part of the page" });
  await expect(alert).toBeVisible();
  const bar = await page.getByRole("region", { name: "Bulk actions" }).boundingBox();
  const at = await alert.boundingBox();
  const table = await page.getByRole("region", { name: "Applications table" }).boundingBox();
  expect(at!.y).toBeGreaterThan(bar!.y);
  expect(at!.y).toBeLessThan(table!.y);
  await page.unroute("**/src/features/catalog/bulk.tsx*");
});
