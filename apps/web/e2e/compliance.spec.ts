import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** The compliance profile switch in Organization settings. Every edition has the profile
 *  (`compliance_profile_available`), and it is off for each organization until an Admin turns it on. While on, records
 *  carry the compliance fields in the form and on the detail page; while off they are hidden.
 *
 *  Turning it on runs in its own organization, made by the Playwright-only `e2e/enterprise-org` route, so the shared
 *  Contoso organization the rest of the suite uses is never changed. */

test("The compliance profile is offered and off by default; records have no compliance fields while it is off", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/admin/organization");
  await expect(admin.getByRole("heading", { level: 1, name: "Organization settings" })).toBeVisible();
  await expect(admin.getByLabel("Use the compliance profile")).not.toBeChecked();

  await admin.goto("/applications/new");
  await expect(admin.getByRole("heading", { level: 1, name: "Add application" })).toBeVisible();
  await expect(admin.getByRole("heading", { name: "Compliance" })).toHaveCount(0);
  await expect(admin.getByLabel("Validation status")).toHaveCount(0);
});

test("An Admin turns the profile on and off; forms and detail follow", async ({ browser, baseURL }) => {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const { email } = await post(contoso, "e2e/enterprise-org", {});
  const page = await pageAs(browser, baseURL, email);
  const t = token();

  await page.goto("/admin/organization");
  const toggle = page.getByLabel("Use the compliance profile");
  await expect(toggle).not.toBeChecked();
  await expectNoViolations(page, "Organization settings with the compliance switch");
  await toggle.check();
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "The compliance profile is on" })).toBeAttached();

  // The application form now has the Compliance section; its values round-trip to the detail page.
  await page.goto("/applications/new");
  await expect(page.getByRole("heading", { name: "Compliance" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(`${t} LIMS`);
  await page.getByLabel("GxP relevant").selectOption({ label: "Yes, GxP relevant" });
  await page.getByLabel("Validation status").selectOption({ label: "Validated" });
  await page.getByLabel("Risk tier").selectOption({ label: "High" });
  await page.getByLabel("Next periodic review").fill("2027-03-01");
  await expectNoViolations(page, "Application form with compliance fields");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/applications\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  const panel = page.getByRole("region", { name: "Compliance" });
  for (const text of ["Yes", "Validated", "High", "2027-03-01"]) await expect(panel).toContainText(text);
  await expectNoViolations(page, "Application detail with compliance fields");

  // The AI agent form has them too.
  await page.goto("/ai-agents/new");
  await expect(page.getByRole("heading", { name: "Compliance" })).toBeVisible();

  // Off again: the fields are hidden everywhere, their values kept.
  await page.goto("/admin/organization");
  await page.getByLabel("Use the compliance profile").uncheck();
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "The compliance profile is off" })).toBeAttached();
  await page.goto(`/applications/${id}`);
  await expect(page.getByRole("heading", { level: 1, name: `${t} LIMS` })).toBeVisible();
  await expect(page.getByRole("region", { name: "Compliance" })).toHaveCount(0);
  await page.goto(`/applications/${id}/edit`);
  await expect(page.getByLabel("Name (required)")).toHaveValue(`${t} LIMS`);
  await expect(page.getByLabel("Validation status")).toHaveCount(0);
  await page.getByRole("button", { name: "Save" }).click(); // an edit while off doesn't touch them
  await expect(page).toHaveURL(`/applications/${id}`);

  await page.goto("/admin/organization");
  await page.getByLabel("Use the compliance profile").check();
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "The compliance profile is on" })).toBeAttached();
  await page.goto(`/applications/${id}`);
  await expect(page.getByRole("region", { name: "Compliance" })).toContainText("Validated");
});
