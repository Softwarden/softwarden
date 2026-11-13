import { readFileSync } from "node:fs";
import { expect, type Browser, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Catalog as code. An Admin exports the catalog (a zip download), plans that same zip back (everything
 *  unchanged, nothing to apply), then plans a single YAML file (zipped in the browser) that creates an application,
 *  reads the field changes, and applies it; an Editor is told the page is for Admins; axe in both themes.
 *
 *  A plan covers the whole catalog (its hash changes with any record), so the spec runs in its own organization
 *  (`e2e/spec-org`, `catalog_code`). */

const ADMIN = "cc-admin@example.test";
const EDITOR = "cc-editor@example.test";

test.describe.configure({ mode: "serial", timeout: 180_000 });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "catalog_code" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const t = token();
const slug = `${t.toLowerCase()}-crm`;

test("An Admin exports the catalog, plans it back unchanged, then plans and applies a new YAML file", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  await page.goto("/");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Catalog as code" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Catalog as code" })).toBeVisible();
  await expectNoViolations(page, "Catalog as code");

  // Export: a zip download, recorded in the audit log.
  await page.getByLabel(/Include people's names and emails/).check();
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download the catalog (zip)" }).click()]);
  expect(download.suggestedFilename()).toMatch(/^softwarden-catalog-.*\.zip$/);
  await expect(page.getByRole("status").filter({ hasText: "The export is recorded in the audit log." })).toHaveCount(1);
  const zip = await download.path();

  // The same tree back: nothing would change, so apply is disabled with the reason beside it.
  await page.getByLabel("Catalog files (required)").setInputFiles({ name: "catalog.zip", mimeType: "application/zip", buffer: readFileSync(zip!) });
  await page.getByRole("button", { name: "Preview the plan" }).click();
  await expect(page.getByTestId("plan-summary")).toContainText("0 to create, 0 to update, 0 to delete");
  await expect(page.getByTestId("plan-summary")).toContainText("0 errors");
  const apply = page.getByRole("button", { name: /^Apply / });
  await expect(apply).toBeDisabled();
  await expect(page.locator("#apply-why")).toHaveText("Nothing would change, so there is nothing to apply.");

  // One YAML file on its own: zipped in the browser, planned, its fields shown, then applied.
  const yaml = `apiVersion: softwarden/v1\nkind: Application\nmetadata:\n  slug: ${slug}\n  name: ${t} CRM\n  description: Customers.\nspec:\n  lifecycle: active\n  criticality: high\n`;
  await page.getByLabel("Catalog files (required)").setInputFiles({ name: "crm.yaml", mimeType: "application/yaml", buffer: Buffer.from(yaml) });
  await expect(page.getByTestId("plan")).toHaveCount(0);
  // Prune is off for loose YAML files, with the reason beside it.
  await expect(page.getByLabel(/Also delete records that have no file/)).toBeDisabled();
  await expect(page.locator("#prune-off")).toContainText("they aren't a whole tree");
  await page.getByRole("button", { name: "Preview the plan" }).click();
  await expect(page.getByTestId("plan-summary")).toHaveText("1 file read: 1 to create, 0 to update, 0 to delete, 0 unchanged, 0 errors.");
  const row = page.getByRole("region", { name: "Planned changes table" }).getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(slug) }) });
  await expect(row).toContainText("Create");
  await row.getByText(/fields of/).click();
  await expect(row.getByTestId("diff-view")).toContainText(`${t} CRM`);
  await expectNoViolations(page, "Catalog as code, plan");

  await page.getByRole("button", { name: "Apply 1 change" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Applied: 1 created, 0 updated, 0 deleted, 0 unchanged." })).toHaveCount(1);
  await expect(page.getByTestId("plan")).toHaveCount(0);
  const found = await (await page.request.get(`/api/v1/applications?q=${encodeURIComponent(`${t} CRM`)}`)).json();
  expect((found.results ?? found).map((a: { name: string }) => a.name)).toContain(`${t} CRM`);
  // Focus goes back to the section's heading, never the body.
  await expect(page.getByRole("heading", { name: "Plan and apply" })).toBeFocused();
  await page.context().close();
});

test("A plan that deletes needs a confirmation; a stale plan and refused files are said in words", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await post(page, "applications", { name: `${t} Keep`, lifecycle: "active" });
  await page.goto("/admin/catalog-code");
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download the catalog (zip)" }).click()]);
  const zip = readFileSync((await download.path())!);
  // A record added after the export has no file, so a pruned plan deletes it.
  await post(page, "applications", { name: `${t} Extra`, lifecycle: "active" });
  await page.getByLabel("Catalog files (required)").setInputFiles({ name: "catalog.zip", mimeType: "application/zip", buffer: zip });
  const prune = page.getByLabel(/Also delete records that have no file/);
  await prune.check();
  await page.getByRole("button", { name: "Preview the plan" }).click();
  await expect(page.getByTestId("plan-summary")).toContainText("1 to delete");
  const apply = page.getByRole("button", { name: "Apply 1 change, deleting 1 item" });
  await expect(apply).toBeDisabled();
  await expect(page.locator("#apply-why")).toHaveText("Tick the box to confirm you've checked the 1 item this deletes.");
  await expectNoViolations(page, "Catalog as code, a plan that deletes");
  await page.getByLabel("I've checked the 1 item this deletes").check();
  await expect(apply).toBeEnabled();

  // The catalog changes after the plan: apply refuses, and says to plan again.
  await post(page, "applications", { name: `${t} Later`, lifecycle: "active" });
  await apply.click();
  await expect(page.getByTestId("plan-stale")).toContainText("nothing was applied");
  await expect(page.getByTestId("plan-stale").getByRole("button", { name: "Plan again" })).toBeVisible();
  await expectNoViolations(page, "Catalog as code, stale plan");
  await page.getByTestId("plan-stale").getByRole("button", { name: "Plan again" }).click();
  await expect(page.getByTestId("plan-summary")).toContainText("2 to delete");
  await expect(page.getByRole("heading", { name: "The plan" })).toBeFocused();

  // A file the apply refuses (answered here: a plan with errors can't be applied) lists each problem.
  await page.route("**/api/v1/catalog-code/apply", (route) => route.fulfill({ status: 400, contentType: "application/problem+json", json: {
    type: "/problems/catalog-code-invalid", title: "Catalog-as-code files have errors", status: 400, detail: "1 problem in the files, so nothing was applied. Fix them and plan again.",
    file_errors: [{ file: "applications/crm.yaml", line: 4, path: "spec.lifecycle", message: "Choose a lifecycle." }],
  } }));
  await page.getByLabel("I've checked the 2 items this deletes").check();
  await page.getByRole("button", { name: "Apply 2 changes, deleting 2 items" }).click();
  await expect(page.getByTestId("apply-errors")).toContainText("applications/crm.yaml, line 4, spec.lifecycle: Choose a lifecycle.");
  await expect(page.getByRole("alert").filter({ hasText: "nothing was applied" })).toHaveCount(1);
  await page.unroute("**/api/v1/catalog-code/apply");
  // A file the server can't read: its reason is on the file input, which gets focus once it is enabled again.
  await page.getByLabel("Catalog files (required)").setInputFiles({ name: "broken.zip", mimeType: "application/zip", buffer: Buffer.from("not a zip") });
  await page.getByRole("button", { name: /Preview the plan|Plan again/ }).click();
  await expect(page.locator("#tree-files-error")).not.toBeEmpty();
  await expect(page.getByLabel("Catalog files (required)")).toBeFocused();
  await expect(page.getByLabel("Catalog files (required)")).toHaveAttribute("aria-describedby", /tree-files-error/);
  // Choosing other files unticks prune.
  await page.getByLabel("Catalog files (required)").setInputFiles({ name: "catalog.zip", mimeType: "application/zip", buffer: zip });
  await expect(prune).not.toBeChecked();
  const still = await (await page.request.get(`/api/v1/applications?q=${encodeURIComponent(`${t} Extra`)}`)).json();
  expect((still.results ?? still).map((a: { name: string }) => a.name)).toContain(`${t} Extra`);
  await page.context().close();
});

test("A prune past the cap is refused with its count until the Admin allows it, then plans and applies", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/admin/catalog-code");
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download the catalog (zip)" }).click()]);
  const zip = readFileSync((await download.path())!);
  // Eleven records the tree doesn't have: more than a prune deletes without asking (10).
  const mass = `${t} Mass`;
  for (let i = 1; i <= 11; i++) await post(page, "applications", { name: `${mass} ${String(i).padStart(2, "0")}`, lifecycle: "active" });
  await page.getByLabel("Catalog files (required)").setInputFiles({ name: "catalog.zip", mimeType: "application/zip", buffer: zip });
  await page.getByLabel(/Also delete records that have no file/).check();
  await expect(page.getByTestId("mass-delete")).toHaveCount(0); // offered only once a plan is refused for it
  await page.getByRole("button", { name: "Preview the plan" }).click();
  await expect(page.getByTestId("plan-errors")).toContainText("This tree with prune deletes 11 records");
  await expect(page.locator("#apply-why")).toHaveText("Fix the errors in the files, then plan again.");
  const allow = page.getByLabel("Allow this prune to delete 11 records");
  await expect(allow).not.toBeChecked();
  await expect(page.getByTestId("mass-delete")).toContainText("a partial checkout, a sparse clone or a truncated zip deletes every record it is missing");
  await expectNoViolations(page, "Catalog as code, a prune past the cap");

  // Allowed: the plan lists the deletes, and applying them (with the flag again) deletes them.
  await allow.check();
  await page.getByRole("button", { name: "Plan again" }).click();
  await expect(page.getByTestId("plan-summary")).toContainText("11 to delete");
  await expect(page.getByTestId("plan-errors")).toHaveCount(0);
  // Taking the permission back drops the plan made with it, so it can't be applied without it.
  await allow.uncheck();
  await expect(page.getByTestId("plan")).toHaveCount(0);
  await allow.check();
  await page.getByRole("button", { name: "Preview the plan" }).click();
  await expect(page.getByTestId("plan-summary")).toContainText("11 to delete");
  await page.getByLabel("I've checked the 11 items this deletes").check();
  await page.getByRole("button", { name: "Apply 11 changes, deleting 11 items" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Applied: 0 created, 0 updated, 11 deleted" })).toBeAttached();
  const left = await (await page.request.get(`/api/v1/applications?q=${encodeURIComponent(mass)}`)).json();
  expect((left.results ?? left).filter((a: { name: string }) => a.name.startsWith(mass))).toEqual([]);
  // Choosing other files forgets the permission.
  await page.getByLabel("Catalog files (required)").setInputFiles({ name: "catalog.zip", mimeType: "application/zip", buffer: zip });
  await expect(page.getByTestId("mass-delete")).toHaveCount(0);
  await page.context().close();
});

test("An Editor is told only Admins can open it", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  await page.goto("/admin/catalog-code");
  await expect(page.getByTestId("admin-only")).toContainText("Only Admins can open Catalog as code.");
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Catalog as code" })).toHaveCount(0);
  await expectNoViolations(page, "Catalog as code (Editor)");
  await page.context().close();
});
