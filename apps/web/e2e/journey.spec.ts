import { expect, test } from "@playwright/test";

// Synthetic demo account created by `manage.py seed_demo`.
const EDITOR = { email: "editor@example.test", password: "softwarden-demo-2026" };

test("Editor creates an application, assigns owners, records a review, and Needs attention clears", async ({ page }) => {
  const name = `Journey App ${Date.now()}`;
  const ownershipSection = page.locator("section", { has: page.getByRole("heading", { name: /^Ownership gaps/ }) });
  const reviewsSection = page.locator("section", { has: page.getByRole("heading", { name: /^Reviews/ }) });

  // Sign in as an Editor.
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to Softwarden" })).toBeVisible();
  await page.getByLabel("Email").fill(EDITOR.email);
  await page.getByLabel("Password").fill(EDITOR.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible(); // everyone's home
  await expect(page.getByRole("button", { name: /Sam Editor · Editor/ })).toBeVisible(); // the sidebar's user menu

  // Create an application with only the required fields.
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications", exact: true }).click();
  await page.getByRole("link", { name: "Add application" }).click();
  await page.getByLabel("Name (required)").fill(name);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  await expect(page.locator("dl.ownership").getByText("No business owner")).toBeVisible();

  // It appears in both of the Editor's queues.
  await page.getByRole("link", { name: "Needs attention" }).click();
  await expect(ownershipSection.getByRole("link", { name, exact: true })).toBeVisible();
  await expect(reviewsSection.getByRole("link", { name, exact: true })).toBeVisible();
  await expect(reviewsSection.getByText("Unreviewed").first()).toBeVisible();

  // Assign its owners.
  await ownershipSection.getByRole("link", { name, exact: true }).click();
  await page.getByRole("button", { name: "Edit ownership" }).click();
  const owners = page.getByRole("dialog", { name: `Assign owners — ${name}` });
  await owners.getByRole("combobox", { name: "Business owner" }).fill("Priya Raman");
  await page.getByRole("option", { name: /^Priya Raman/ }).first().click();
  await owners.getByRole("combobox", { name: "Technical owner" }).fill("Dana Ortiz");
  await page.getByRole("option", { name: /^Dana Ortiz/ }).first().click();
  await owners.getByRole("combobox", { name: "Support team" }).fill("IT Applications");
  await page.getByRole("option", { name: "IT Applications", exact: true }).click();
  await owners.getByLabel("Criticality").selectOption({ label: "High" });
  await owners.getByRole("button", { name: "Save" }).click();
  await expect(owners).toBeHidden();
  await expect(page.getByRole("status").filter({ hasText: "Ownership saved." })).toBeAttached();
  await expect(page.locator("dl.ownership").getByText("Dana Ortiz")).toBeVisible();

  // Ownership gaps clear; the review item remains.
  await page.getByRole("link", { name: "Needs attention" }).click();
  await expect(reviewsSection.getByRole("link", { name, exact: true })).toBeVisible();
  await expect(ownershipSection.getByRole("link", { name, exact: true })).toHaveCount(0);

  // Record a review.
  await reviewsSection.getByRole("link", { name, exact: true }).click();
  await page.getByRole("button", { name: "Record review" }).click();
  const review = page.getByRole("dialog", { name: `Record review — ${name}` });
  await review.getByLabel("Confirmed by (required)").selectOption({ label: "Dana Ortiz" });
  await review.getByLabel("Evidence / reference (required)").fill("Email from Dana, synthetic demo");
  await review.getByRole("button", { name: "Record review" }).click();
  await expect(review).toBeHidden();
  await expect(page.locator(".meta-line")).toContainText("Confirmed");
  // The Review card on the Overview tab shows the latest review.
  await expect(page.getByRole("region", { name: "Review", exact: true }).getByText("Dana Ortiz")).toBeVisible();

  // Both Needs attention items have cleared.
  await page.getByRole("link", { name: "Needs attention" }).click();
  await expect(page.getByRole("heading", { name: "Needs attention" })).toBeVisible();
  await expect(reviewsSection).toBeVisible();
  await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
});

test("A Viewer sees records but no change buttons", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("viewer@example.test");
  await page.getByLabel("Password").fill(EDITOR.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible(); // everyone's home
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Applications" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Add application" })).toHaveCount(0);
});
