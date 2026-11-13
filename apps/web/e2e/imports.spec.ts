import { expectNoViolations } from "./axe";
import { expect, test, type Page } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** CSV import: upload, map columns, preview with decisions, commit, refusal, results, import sources. */

// axe runs in the light and the dark theme at every check (e2e/axe.ts), so these tests get three minutes.
test.describe.configure({ mode: "serial", timeout: 180_000 });

const csv = (headers: string[], rows: string[][]) => [headers, ...rows].map((r) => r.join(",")).join("\n") + "\n";

/** Open New import, choose a file and upload it. Leaves the page on the run. */
async function upload(page: Page, name: string, content: string | Buffer, source?: string) {
  await page.goto("/imports");
  await page.getByRole("link", { name: "New import" }).first().click();
  await expect(page.getByRole("heading", { level: 2, name: "Upload" })).toBeVisible();
  if (source) await page.getByLabel("Import source").selectOption({ label: source });
  await page.getByLabel("CSV file").setInputFiles({ name, mimeType: "text/csv", buffer: Buffer.isBuffer(content) ? content : Buffer.from(content) });
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
}

const stepHeading = (page: Page, name: string | RegExp) => page.getByRole("heading", { level: 2, name });
const commitButton = (page: Page) => page.getByRole("button", { name: /^(Commit \d+ changes?|Retry commit)$/ });

const t = token();

test("Upload the template: create two applications, choose Create person for an unmatched owner, commit, see results", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.goto("/imports");
  await expect(page.getByRole("heading", { level: 1, name: "Imports" })).toBeVisible();
  await page.getByRole("link", { name: "New import" }).first().click();
  await expect(stepHeading(page, "Upload")).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: "Upload" }).first()).toHaveAttribute("aria-current", "step");
  await expectNoViolations(page, "Import upload step");

  // The template downloads as a file.
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: /Download template/ }).click()]);
  expect(download.suggestedFilename()).toMatch(/template\.csv$/);

  // A file with no name column is refused with a message beside the file field.
  await page.getByLabel("CSV file").setInputFiles({ name: "empty.csv", mimeType: "text/csv", buffer: Buffer.from("") });
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /Fix (this|these)/ })).toBeVisible();
  await expectNoViolations(page, "Import upload step with a rejected file");

  const owner = `${t}.alpha@example.test`;
  const file = csv(["name", "lifecycle", "business_owner_email", "business_owner_name"], [[`${t} Alpha`, "active", owner, "Alpha Owner"], [`${t} Beta`, "active", "", ""]]);
  await page.getByLabel("CSV file").setInputFiles({ name: "apps.csv", mimeType: "text/csv", buffer: Buffer.from(file) });
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: "Preview" }).first()).toHaveAttribute("aria-current", "step");

  // The unmatched owner needs a choice, and Commit says why it is off.
  const people = page.getByRole("region", { name: "Owners, teams and vendors" });
  await expect(people).toContainText(owner);
  await expect(commitButton(page)).toBeDisabled();
  await expect(page.getByText(/choice still needed/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Create 2" })).toBeVisible();
  await expectNoViolations(page, "Import preview step with a pending choice");
  await people.getByRole("radio", { name: /Create person/ }).check();
  await expect(commitButton(page)).toBeEnabled();
  await expect(commitButton(page)).toHaveText("Commit 2 changes");
  await expectNoViolations(page, "Import preview step");

  await commitButton(page).click();
  await expect(stepHeading(page, "Results")).toBeVisible();
  await expect(page.getByText("Import committed")).toBeVisible();
  await expect(page.getByRole("link", { name: "Review gaps in Needs attention" })).toBeVisible();
  await expect(page.getByRole("link", { name: `${t} Alpha` })).toBeVisible();
  await expectNoViolations(page, "Import results step");

  // History lists it as Committed.
  await page.goto("/imports");
  await expect(page.getByRole("row", { name: /apps\.csv/ }).first()).toContainText("Committed");
  await expect(page.getByRole("row", { name: /apps\.csv/ }).first()).toContainText("2 created");
  await expectNoViolations(page, "Imports history");
});

test("Re-uploading the export unchanged says there is nothing to commit", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const res = await page.request.get(`/api/v1/applications/export.csv?q=${t}`);
  expect(res.ok()).toBeTruthy();
  // Search is fuzzy, so other tests' records can be in the export: keep the header and this test's two rows.
  const [header, ...lines] = (await res.body()).toString("utf8").split("\r\n").filter(Boolean);
  const mine = lines.filter((line) => line.includes(`${t} `));
  expect(mine).toHaveLength(2);
  const body = Buffer.from([header, ...mine, ""].join("\r\n"), "utf8");
  await upload(page, "export.csv", body);
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await expect(page.getByText("All 2 rows are unchanged. Nothing to commit.")).toBeVisible();
  await expect(commitButton(page)).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Done" })).toBeVisible();
  await expectNoViolations(page, "Import preview with nothing to commit");
});

test("An error row blocks Commit until it is excluded; the error report downloads", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t2 = token();
  await upload(page, "errors.csv", csv(["name", "lifecycle"], [[`${t2} Good`, "active"], [`${t2} Bad`, "Live"]]));
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await expect(commitButton(page)).toBeDisabled();
  await expect(page.getByText(/1 row has errors/)).toBeVisible();
  await expect(page.getByText(/Lifecycle/).first()).toBeVisible();

  // The Error tile filters the rows and is a toggle button kept in the URL.
  await page.getByRole("button", { name: /^Error \d/ }).click();
  await expect(page).toHaveURL(/outcome=error/);
  await expect(page.getByRole("button", { name: /^Error \d/ })).toHaveAttribute("aria-pressed", "true");
  const rows = page.getByRole("region", { name: "Import rows table" });
  await expect(rows.getByRole("row")).toHaveCount(2); // header + the bad row
  await expectNoViolations(page, "Import preview with an error row");

  const exclude = page.getByRole("checkbox", { name: new RegExp(`Exclude row \\d+, ${t2} Bad`) });
  await exclude.check();
  await expect(page.getByRole("status").filter({ hasText: /excluded/ })).toBeVisible();
  await expect(commitButton(page)).toBeEnabled();
  await expect(commitButton(page)).toHaveText("Commit 1 change");

  await commitButton(page).click();
  await expect(stepHeading(page, "Results")).toBeVisible();
  await expect(page.getByText(/Kept until/)).toBeVisible();
  const [report] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download error report" }).click()]);
  expect(report.suggestedFilename()).toMatch(/errors\.csv$/);
});

test("A possible match needs a choice before Commit", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t3 = token();
  await post(page, "applications", { name: `${t3} Twin`, lifecycle: "active" });
  await upload(page, "twin.csv", csv(["name", "lifecycle"], [[`${t3} Twin`, "active"]]));
  await expect(stepHeading(page, "Preview")).toBeVisible();
  const decide = page.getByRole("region", { name: /Needs your decision/ });
  await expect(decide).toContainText("Names never merge records");
  await expect(commitButton(page)).toBeDisabled();
  await expectNoViolations(page, "Import preview with a possible match");
  await expect(decide.getByRole("radio", { name: /^Match to/ })).not.toBeChecked();
  await decide.getByRole("radio", { name: "Create a new record" }).check();
  await expect(commitButton(page)).toBeEnabled();
  await commitButton(page).click();
  await expect(stepHeading(page, "Results")).toBeVisible();
  await expect(page.getByText(/Created\s*1/)).toBeVisible();
});

test("A commit is refused when a matched record changed after the preview; Rerun preview recovers", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t4 = token();
  const app = await post(page, "applications", { name: `${t4} Stable`, lifecycle: "active" });
  await upload(page, "update.csv", csv(["softwarden_id", "name", "lifecycle"], [[app.id, `${t4} Stable`, "deprecated"]]));
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await expect(page.getByText("→").first()).toBeVisible(); // before → after
  await expect(commitButton(page)).toBeEnabled();

  await post(page, `applications/${app.id}`, { description: "Changed behind the preview" }, "PATCH");
  await commitButton(page).click();
  const refused = page.getByRole("alert").filter({ hasText: "Nothing was imported" });
  await expect(refused).toBeVisible();
  await expect(refused).toContainText(`${t4} Stable`);
  await expect(stepHeading(page, "Commit")).toBeVisible();
  await expectNoViolations(page, "Import refused");

  await page.getByRole("button", { name: "Rerun preview" }).click();
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await expect(commitButton(page)).toBeEnabled();
  await commitButton(page).click();
  await expect(stepHeading(page, "Results")).toBeVisible();
});

test("A file with other headings goes to Map columns; the mapping is saved for the source and pre-filled next time", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const t5 = token();
  const sourceName = `${t5} Sheet`;
  await post(admin, "import-sources", { name: sourceName, kind: "application" });

  const page = await pageAs(browser, baseURL, "editor@example.test");
  const file = csv(["Application", "Stage", "Contact"], [[`${t5} Mapped`, "active", "someone"]]);
  await upload(page, "sheet.csv", file, sourceName);
  await expect(stepHeading(page, "Map columns")).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: "Map columns" }).first()).toHaveAttribute("aria-current", "step");
  await expectNoViolations(page, "Import map columns step");

  // Name is required: continuing with nothing mapped to it is stopped.
  await page.getByLabel("Field for column Application").selectOption("ignore");
  await page.getByLabel("Field for column Stage").selectOption("lifecycle");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /Name/ })).toBeVisible();

  await page.getByLabel("Field for column Application").selectOption("name");
  await page.getByLabel(`Save this mapping for ${sourceName}`).check();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await expect(page.getByText(`${t5} Mapped`).first()).toBeVisible();
  await commitButton(page).click();
  await expect(stepHeading(page, "Results")).toBeVisible();

  // The next file from the same source starts from the saved mapping (or skips the step if it fully applies).
  await upload(page, "sheet2.csv", csv(["Application", "Stage", "Contact"], [[`${t5} Mapped Again`, "active", "someone"]]), sourceName);
  await expect(page.getByRole("heading", { level: 2, name: /Map columns|Preview/ })).toBeVisible();
  if (await stepHeading(page, "Map columns").isVisible()) {
    await expect(page.getByLabel("Field for column Application")).toHaveValue("name");
    await expect(page.getByLabel("Field for column Stage")).toHaveValue("lifecycle");
  }
});

test("Admin manages import sources: add, rename, delete", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const t6 = token();
  await page.goto("/admin/import-sources");
  await expect(page.getByRole("heading", { level: 1, name: "Import sources" })).toBeVisible();
  await expectNoViolations(page, "Import sources page");
  await page.getByRole("button", { name: "Add source" }).first().click();
  const add = page.getByRole("dialog", { name: "Add import source" });
  await add.getByRole("button", { name: "Add source" }).click();
  await expect(add.getByRole("alert")).toBeVisible();
  await add.getByLabel("Name (required)").fill(`${t6} Old`);
  await add.getByRole("button", { name: "Add source" }).click();
  const table = page.getByRole("region", { name: "Import sources table" });
  await expect(table.getByRole("row", { name: new RegExp(`${t6} Old`) })).toBeVisible();

  await table.getByRole("button", { name: `Rename ${t6} Old` }).click();
  const rename = page.getByRole("dialog", { name: /^Rename/ });
  await rename.getByLabel("Name (required)").fill(`${t6} New`);
  await rename.getByRole("button", { name: "Save name" }).click();
  await expect(table.getByRole("row", { name: new RegExp(`${t6} New`) })).toBeVisible();
  await expect(page.getByRole("button", { name: `Rename ${t6} New` })).toBeFocused();

  await table.getByRole("button", { name: `Delete ${t6} New` }).click();
  await page.getByRole("dialog", { name: /^Delete/ }).getByRole("button", { name: "Delete source" }).click();
  await expect(table.getByRole("row", { name: new RegExp(`${t6} New`) })).toHaveCount(0);
});

test("Viewers can't open Imports or Import sources", async ({ browser, baseURL }) => {
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/imports");
  await expect(viewer.getByText("Imports are for Editors and Admins")).toBeVisible();
  await expect(viewer.getByRole("link", { name: "New import" })).toHaveCount(0);
  await viewer.goto("/imports/new");
  await expect(viewer.getByText("Imports are for Editors and Admins")).toBeVisible();
  await expectNoViolations(viewer, "Imports as a Viewer");
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await editor.goto("/admin/import-sources");
  await expect(editor.getByRole("heading", { name: "Page not found" })).toBeVisible(); // Admin-only route, like the other admin pages
});
