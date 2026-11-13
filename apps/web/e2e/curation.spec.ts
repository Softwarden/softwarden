import { expectNoViolations } from "./axe";
import { expect, test, type Page } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** Curation tools on the Applications and Components lists: tags, saved filters, bulk edit and export. */

// axe runs in the light and the dark theme at every check (e2e/axe.ts), so these tests get three minutes.
test.describe.configure({ mode: "serial", timeout: 180_000 });

/** A save notice: the announced status text (the visible copy beside it is aria-hidden). */
const said = (page: Page, text: string | RegExp) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeAttached();

test("Tags: add on create, see them on the record and in the list, filter by tag", async ({ browser, baseURL }) => {
  const t = token();
  const tag = `Fin${t}`;
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await post(page, "applications", { name: `${t} Untagged`, lifecycle: "active" });

  await page.goto("/applications/new");
  await page.getByLabel("Name (required)").fill(`${t} Ledger`);
  const input = page.getByLabel("Tags", { exact: true });
  await input.fill(tag);
  await input.press("Enter");
  await expect(page.getByRole("list", { name: "Tags added" })).toContainText(tag);
  await input.fill(`${tag.toLowerCase()}`); // same tag, different case: not added twice
  await input.press("Enter");
  await expect(page.getByRole("list", { name: "Tags added" }).getByRole("listitem")).toHaveCount(1);
  await input.fill("Quarterly,");
  await expect(page.getByRole("button", { name: "Remove tag Quarterly" })).toBeVisible();
  await page.getByRole("button", { name: "Remove tag Quarterly" }).click();
  await expect(page.getByRole("button", { name: "Remove tag Quarterly" })).toHaveCount(0);
  await expectNoViolations(page, "Add application with a tag");
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page.getByRole("heading", { level: 1, name: `${t} Ledger` })).toBeVisible();
  await expect(page.getByRole("list", { name: "Tags" })).toContainText(tag);

  await page.goto(`/applications?q=${t}`);
  const table = page.getByRole("region", { name: "Applications table" });
  await expect(table.getByRole("link", { name: `${t} Untagged` })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Ledger`) })).toContainText(tag);
  // The tag filter is a chip in the FilterBar: Add filter, then choose the tag.
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Tag" }).click();
  await expect(page.getByLabel("Tag", { exact: true })).toBeFocused();
  await page.getByLabel("Tag", { exact: true }).selectOption(tag);
  await expect(page).toHaveURL(new RegExp(`tag=${tag}`));
  await expect(table.getByRole("link", { name: `${t} Untagged` })).toHaveCount(0);
  await expect(table.getByRole("link", { name: `${t} Ledger` })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Tag", { exact: true })).toHaveValue(tag);
});

test("Saved views: a built-in, then save, apply and delete a shared view", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await post(page, "applications", { name: `${t} Bare`, lifecycle: "active" });
  await page.goto(`/applications?q=${t}`);
  const table = page.getByRole("region", { name: "Applications table" });
  await expect(table.getByRole("link", { name: `${t} Bare` })).toBeVisible();

  // Built-in: Missing owners applies its query to the URL.
  const select = page.getByLabel("Saved views", { exact: true });
  await expect(select.locator("optgroup[label='Built-in'] option")).toHaveText(["Missing owners", "Overdue reviews", "Unreviewed imports", "Active"]);
  await select.selectOption({ label: "Missing owners" });
  await expect(page).toHaveURL(/gap=true/);
  await expect(page).not.toHaveURL(/q=/);
  await expect(select).toHaveValue("builtin:missing-owners");
  await expect(page.getByRole("button", { name: "Missing owners" })).toHaveAttribute("aria-pressed", "false");

  // Save the current view (Overdue reviews preset plus a search) as a personal filter.
  await page.getByRole("button", { name: "Overdue reviews" }).click();
  await page.getByLabel("Search applications").fill(t);
  await expect(page).toHaveURL(new RegExp(`q=${t}`));
  await page.getByRole("button", { name: "Save as view…" }).click();
  const dialog = page.getByRole("dialog", { name: "Save as view" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/Lifecycle: Active, Deprecated/)).toBeVisible();
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Enter a name.");
  await expectNoViolations(page, "Save as view dialog with an error");
  const name = `View ${t}`;
  await dialog.getByLabel("Name (required)").fill(name);
  await dialog.getByLabel("Share with everyone").check(); // Editors may share
  await expectNoViolations(page, "Save as view dialog");
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save as view…" })).toBeFocused();
  await said(page, `Saved view “${name}”, shared with everyone.`);
  await expect(select.locator(`optgroup[label='Shared with everyone'] option`, { hasText: name })).toHaveCount(1);
  await expect(select).toHaveValue(/[0-9a-f-]{36}/);

  // Elsewhere, choose it: the URL takes its query.
  await page.goto("/applications");
  await page.getByLabel("Saved views", { exact: true }).selectOption({ label: name });
  await expect(page).toHaveURL(/review_state=overdue/);
  await expect(page).toHaveURL(new RegExp(`q=${t}`));
  await expect(page.getByLabel("Search applications")).toHaveValue(t);

  // Another role sees the shared filter; a Viewer can still save a personal one.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/applications");
  await expect(viewer.getByLabel("Saved views", { exact: true }).locator("option", { hasText: name })).toHaveCount(1);
  await expect(viewer.getByRole("button", { name: "Save as view…" })).toBeVisible();
  await viewer.getByRole("button", { name: "Save as view…" }).click();
  await expect(viewer.getByLabel("Share with everyone")).toHaveCount(0);

  // Delete it.
  await page.getByRole("region", { name: `View: ${name}` }).getByRole("button", { name: "Delete view…" }).click();
  const confirm = page.getByRole("dialog", { name: "Delete saved view?" });
  await expectNoViolations(page, "Delete saved view dialog");
  await confirm.getByRole("button", { name: `Delete “${name}”` }).click();
  await expect(confirm).toHaveCount(0);
  await said(page, `Deleted saved view “${name}”.`);
  await expect(page.getByLabel("Saved views", { exact: true }).locator("option", { hasText: name })).toHaveCount(0);
});

test("Bulk edit: preview shows who needs confirmation, apply changes the technical owner on two records", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const people: { id: string; name: string }[] = await (await page.request.get("/api/v1/people?active=true")).json();
  const [first, second] = people;
  const teams: { id: string }[] = await (await page.request.get("/api/v1/teams")).json();
  const ids: string[] = [];
  for (const n of ["A", "B"]) {
    const app = await post(page, "applications", {
      name: `${t} ${n}`, lifecycle: "active", criticality: "high", business_owner: first.id, technical_owner: first.id, support_team: teams[0].id,
    });
    await post(page, `applications/${app.id}/reviews`, { confirmed_by: first.id, evidence: "Synthetic" });
    ids.push(app.id);
  }

  await page.goto(`/applications?q=${t}`);
  const table = page.getByRole("region", { name: "Applications table" });
  await expect(table.getByRole("link", { name: `${t} B` })).toBeVisible();
  await expect(page.getByRole("button", { name: "Bulk edit…" })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: "Select all on this page" })).toBeVisible();
  await page.getByRole("checkbox", { name: `Select ${t} A` }).check();
  await page.getByRole("checkbox", { name: `Select ${t} B` }).check();
  await expect(page.getByRole("region", { name: "Bulk actions" })).toContainText("2 selected");
  await page.getByRole("button", { name: "Bulk edit…" }).click();
  const dialog = page.getByRole("dialog", { name: "Bulk edit 2 applications" });
  await expect(dialog).toBeVisible();

  // Nothing chosen yet.
  await dialog.getByRole("button", { name: "Preview changes" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Choose at least one change.");

  const owner = dialog.getByRole("combobox", { name: "Technical owner" });
  await owner.click();
  await page.getByRole("option", { name: new RegExp(second.name) }).first().click();
  await dialog.getByLabel("Next review date").fill("2027-06-30");
  await expectNoViolations(page, "Bulk edit dialog, choose changes");
  await dialog.getByRole("button", { name: "Preview changes" }).click();

  await expect(dialog.getByRole("status").filter({ hasText: "will change" })).toContainText("2 records will change; 2 will need confirmation");
  const preview = dialog.getByRole("region", { name: "Bulk edit preview table" });
  for (const n of ["A", "B"]) {
    const row = preview.getByRole("row", { name: new RegExp(`${t} ${n}`) });
    await expect(row).toContainText("Technical owner:");
    await expect(row).toContainText(first.name);
    await expect(row).toContainText(second.name);
    await expect(row).toContainText("Next review date:");
    await expect(row).toContainText("Will need confirmation");
  }
  await expectNoViolations(page, "Bulk edit dialog, preview");

  await dialog.getByRole("button", { name: "Apply to 2 records" }).click();
  await expect(dialog).toHaveCount(0);
  await said(page, "Updated 2 records; 0 unchanged.");
  await expect(table.getByRole("link", { name: `${t} A` })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: "Applications" })).toBeFocused();
  for (const id of ids) {
    const rec = await (await page.request.get(`/api/v1/applications/${id}`)).json();
    expect(rec.technical_owner.id).toBe(second.id);
    expect(rec.review_state).toBe("needs_confirmation");
  }

  // Applying the same change again: every row is unchanged and there is nothing to apply.
  await page.getByRole("checkbox", { name: `Select ${t} A` }).check();
  await page.getByRole("button", { name: "Bulk edit…" }).click();
  await page.getByRole("combobox", { name: "Technical owner" }).click();
  await page.getByRole("option", { name: new RegExp(second.name) }).first().click();
  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(page.getByRole("button", { name: "Nothing to apply" })).toBeDisabled();
  await expect(page.getByRole("dialog")).toContainText("Unchanged");
  await page.keyboard.press("Escape");

  // A Viewer gets no selection boxes.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications?q=${t}`);
  await expect(viewer.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} A` })).toBeVisible();
  await expect(viewer.getByRole("checkbox")).toHaveCount(0);
  await expect(viewer.getByRole("link", { name: "Download template" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: "Export CSV…" })).toBeVisible();
});

test("Export: the dialog downloads a CSV with a BOM, the escape and the tag; the template downloads for Editors", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await post(page, "applications", { name: `${t} Export`, lifecycle: "active", purpose: "=SUM(A1)", tags: [`Tag${t}`] });
  const old = await post(page, "applications", { name: `${t} Archived`, lifecycle: "retired" });
  await post(page, `applications/${old.id}/archive`, {});

  await page.goto(`/applications?q=${t}`);
  await expect(page.getByRole("region", { name: "Applications table" }).getByRole("link", { name: `${t} Export` })).toBeVisible();
  await page.getByRole("button", { name: "Export CSV…" }).click();
  const dialog = page.getByRole("dialog", { name: "Export CSV" });
  await expect(dialog).toContainText("prefixed with a '");
  await expect(dialog).toContainText("UTF-8 with a BOM");
  await expect(dialog).toContainText("recorded in the audit log");
  await expectNoViolations(page, "Export dialog");
  const [download] = await Promise.all([page.waitForEvent("download"), dialog.getByRole("button", { name: "Download CSV" }).click()]);
  expect(download.suggestedFilename()).toMatch(/^softwarden-applications-\d{4}-\d{2}-\d{2}\.csv$/);
  const body = await (await import("node:fs/promises")).readFile((await download.path())!);
  expect([...body.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  const text = body.toString("utf8");
  expect(text).toContain(`${t} Export`);
  expect(text).toContain("'=SUM(A1)");
  expect(text).toContain(`Tag${t}`);
  expect(text).not.toContain(`${t} Archived`); // current filters: the search for this token, archived excluded
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Export CSV…" })).toBeFocused();
  await said(page, "recorded in the audit log");

  // Also reachable from a Viewer.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  const res = await viewer.request.get(`/api/v1/applications/export.csv?q=${t}`);
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("text/csv");
  expect(res.headers()["content-disposition"]).toContain("attachment");

  // Template: Editors only.
  const [tpl] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name: "Download template" }).click()]);
  expect(tpl.suggestedFilename()).toBe("softwarden-applications-template.csv");
  const head = (await (await import("node:fs/promises")).readFile((await tpl.path())!)).toString("utf8");
  expect(head).toContain("name");
});

test("Components list has the same tools; axe on the list with the dialogs open", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await post(page, "components", { name: `${t} Job`, lifecycle: "active", component_type: "scheduled_job", tags: [`Batch${t}`] });
  await page.goto(`/applications?kind=components&q=${t}`);
  const table = page.getByRole("region", { name: "Components table" });
  await expect(table.getByRole("row", { name: new RegExp(`${t} Job`) })).toContainText(`Batch${t}`);
  await expectNoViolations(page, "Components list with tags and saved filters");

  await page.getByRole("checkbox", { name: `Select ${t} Job` }).check();
  await expectNoViolations(page, "Components list with a selection");
  await page.getByRole("button", { name: "Bulk edit…" }).click();
  await expect(page.getByRole("dialog", { name: "Bulk edit 1 component" })).toBeVisible();
  await page.getByRole("combobox", { name: "Support team" }).click();
  await expectNoViolations(page, "Bulk edit dialog with a list open");
  await page.keyboard.press("Escape"); // closes the list
  await page.keyboard.press("Escape"); // closes the dialog
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Bulk edit…" })).toBeFocused();

  // The bulk bar replaces the header's actions while rows are selected; clearing brings Export back.
  await expect(page.getByRole("button", { name: "Export CSV…" })).toHaveCount(0);
  await page.getByRole("button", { name: "Clear selection" }).click();
  await expect(page.getByRole("checkbox", { name: "Select all on this page" })).toBeFocused();
  await page.getByRole("button", { name: "Export CSV…" }).click();
  await expect(page.getByRole("dialog", { name: "Export CSV" })).toContainText("Components");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Export CSV…" })).toBeFocused();

  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Tag" }).click();
  await page.getByLabel("Tag", { exact: true }).selectOption(`Batch${t}`);
  await expect(table.getByRole("link", { name: `${t} Job` })).toBeVisible();
  await page.getByLabel("Tag", { exact: true }).selectOption("");
  await page.getByLabel("Saved views", { exact: true }).selectOption({ label: "Active" });
  await expect(page).toHaveURL(/lifecycle=active%2Cdeprecated/);
});
