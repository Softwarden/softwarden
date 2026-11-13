import { expect, test } from "@playwright/test";

/** The first run, end to end in one session: assign a technical
 *  owner who has no login, find missing ownership on Needs attention and record a confirmation, see the review history
 *  and next review date, and read the application and its reviews through the API. Deploying and signing in is the
 *  README install path; permissions, tenancy and audit are the pytest suites. */

const PASSWORD = "softwarden-demo-2026"; // synthetic demo accounts from seed_demo

test("First run: add an application, assign an owner without a login, record a review, read it back through the API", async ({ page }) => {
  const stamp = Date.now();
  const name = `First Run ${stamp}`;
  const owner = `Morgan Lee ${stamp}`;
  const ownershipSection = page.locator("section", { has: page.getByRole("heading", { name: /^Ownership gaps/ }) });

  await page.goto("/login");
  await page.getByLabel("Email").fill("editor@example.test");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible(); // everyone's home

  // 1. Add application with name and lifecycle only.
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications", exact: true }).click();
  await page.getByRole("link", { name: "Add application" }).first().click();
  await page.getByLabel("Name (required)").fill(name);
  await page.getByRole("combobox", { name: "Business owner" }).fill("Priya Raman");
  await page.getByRole("option", { name: /^Priya Raman/ }).first().click();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();

  // 2. Find the missing ownership on Needs attention.
  await page.getByRole("link", { name: "Needs attention" }).click();
  await expect(ownershipSection.getByRole("link", { name, exact: true })).toBeVisible();
  await ownershipSection.getByRole("link", { name, exact: true }).click();

  // 3. The gap's Assign opens the dialog on that field; add the owner inline. They have no login.
  await page.getByRole("button", { name: "Assign technical owner" }).click();
  const dialog = page.getByRole("dialog", { name: `Assign owners — ${name}` });
  await expect(dialog.getByRole("combobox", { name: "Technical owner" })).toBeFocused();
  await dialog.getByRole("combobox", { name: "Technical owner" }).fill(owner);
  await page.getByRole("option", { name: `+ Add person "${owner}"` }).click();
  await dialog.getByRole("button", { name: "Add person" }).first().click();
  await expect(dialog.getByRole("combobox", { name: "Technical owner" })).toHaveValue(owner);
  await dialog.getByRole("combobox", { name: "Support team" }).fill("Service Desk");
  await page.getByRole("option", { name: "Service Desk", exact: true }).click();
  await dialog.getByLabel("Criticality").selectOption("medium");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Ownership saved." })).toBeVisible();
  await expect(page.locator("dl.ownership").getByText(owner)).toBeVisible();

  // 4. Record the confirmation obtained by email; the record is Confirmed.
  await page.getByRole("button", { name: "Record review" }).click();
  const review = page.getByRole("dialog", { name: `Record review — ${name}` });
  await review.getByLabel("Confirmed by (required)").selectOption({ label: owner });
  await review.getByLabel("Evidence / reference (required)").fill(`Email from ${owner}`);
  await review.getByRole("button", { name: "Record review" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Review recorded." })).toBeAttached();

  // 5. Review history and the next review date.
  await page.getByRole("tab", { name: /^Review history/ }).click();
  const history = page.getByRole("region", { name: "Review history table" });
  await expect(history.getByRole("cell", { name: `Email from ${owner}` })).toBeVisible();
  await expect(history.getByRole("cell", { name: owner, exact: true })).toBeVisible();

  // Needs attention has nothing left for this record.
  await page.getByRole("link", { name: "Needs attention" }).click();
  await expect(page.getByRole("heading", { name: "Needs attention" })).toBeVisible();
  await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);

  // 6. Read the application and its reviews through the session API.
  const list = await (await page.request.get(`/api/v1/applications?q=${encodeURIComponent(name)}`)).json();
  const app = list.results.find((r: { name: string }) => r.name === name);
  expect(app.review_state).toBe("confirmed");
  expect(app.technical_owner.name).toBe(owner);
  expect(app.next_review_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  const reviews = await (await page.request.get(`/api/v1/applications/${app.id}/reviews`)).json();
  expect(reviews).toHaveLength(1);
  expect(reviews[0].confirmed_values.technical_owner).toBe(app.technical_owner.id);
});
