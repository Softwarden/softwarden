import { expect, test, type Page } from "@playwright/test";

const PASSWORD = "softwarden-demo-2026"; // synthetic demo accounts from seed_demo

async function signIn(page: Page, email = "editor@example.test") {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible(); // everyone's home
}

test("Editor creates an application with an inline vendor and person; a duplicate name warns before saving", async ({ page }) => {
  const stamp = Date.now();
  const name = `Expense Hub ${stamp}`;
  await signIn(page);
  await page.goto("/applications");
  await page.getByRole("link", { name: "Add application" }).first().click();
  await expect(page.getByRole("heading", { name: "Add application" })).toBeVisible();

  // Only name and lifecycle are required.
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("alert")).toContainText("Enter a name.");

  await page.getByLabel("Name (required)").fill(name);
  await page.getByLabel("Criticality").selectOption("high");

  await page.getByRole("combobox", { name: "Vendor" }).fill(`Contoso Labs ${stamp}`);
  await page.getByRole("option", { name: `+ Add vendor "Contoso Labs ${stamp}"` }).click();
  await expect(page.getByRole("status").filter({ hasText: `Vendor Contoso Labs ${stamp} added and selected.` })).toBeAttached();

  await page.getByRole("combobox", { name: "Business owner" }).fill(`Robin ${stamp}`);
  await page.getByRole("option", { name: `+ Add person "Robin ${stamp}"` }).click();
  await page.getByLabel("Email of new person").fill(`robin.${stamp}@example.test`);
  await page.getByRole("group", { name: "Add person" }).getByRole("button", { name: "Add person" }).click();
  await expect(page.getByRole("combobox", { name: "Business owner" })).toHaveValue(`Robin ${stamp}`);

  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  await expect(page.getByText(`Vendor: Contoso Labs ${stamp}`)).toBeVisible();

  // The same name again is a warning, not a block; Save anyway creates a second record.
  await page.goto("/applications/new");
  await page.getByLabel("Name (required)").fill(name.toLowerCase());
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").filter({ hasText: `Another application is named ${name}. Save anyway?` })).toBeVisible();
  await page.getByRole("button", { name: "Save anyway" }).click();
  await expect(page.getByRole("heading", { name: name.toLowerCase(), level: 1 })).toBeVisible();
  expect(page.url()).toMatch(/\/applications\/[0-9a-f-]{36}$/);
});

test("Editor creates a component, then edits it; changing a confirmed field on a Confirmed record shows the needs-confirmation note", async ({ page }) => {
  const name = `Nightly sync ${Date.now()}`;
  await signIn(page);
  await page.goto("/components/new");
  await expect(page).toHaveURL(/\/applications\/new\?kind=component$/);
  await expect(page.getByRole("radio", { name: "Component" })).toBeChecked();
  await page.getByLabel("Name (required)").fill(name);
  await page.getByLabel("Component type").selectOption("scheduled_job");
  await page.getByLabel("Host / runtime").fill("Azure Functions");
  await page.getByRole("combobox", { name: "Business owner" }).fill("Dana Ortiz");
  await page.getByRole("option", { name: /^Dana Ortiz/ }).first().click();
  await page.getByRole("combobox", { name: "Technical owner" }).fill("Priya Raman");
  await page.getByRole("option", { name: /^Priya Raman/ }).first().click();
  await page.getByRole("combobox", { name: "Support team" }).fill("Service Desk");
  await page.getByRole("option", { name: "Service Desk", exact: true }).click();
  await page.getByLabel("Criticality").selectOption("medium");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  await expect(page).toHaveURL(/\/components\//);

  // Record a review so the record is Confirmed.
  await page.getByRole("button", { name: "Record review" }).click();
  const review = page.getByRole("dialog", { name: `Record review — ${name}` });
  await review.getByLabel("Confirmed by (required)").selectOption({ label: "Dana Ortiz" });
  await review.getByLabel("Evidence / reference (required)").fill("Email from Dana");
  await review.getByRole("button", { name: "Record review" }).click();

  await page.getByRole("link", { name: `Edit ${name}` }).click();
  await expect(page.getByRole("heading", { name: `Edit ${name}` })).toBeVisible();
  await expect(page.getByText("Component (can't be changed)")).toBeVisible();
  await page.getByLabel("Host / runtime").fill("Azure Functions (EU)");
  await expect(page.getByText("will mark this record Needs confirmation")).toHaveCount(0); // not a confirmed field
  await page.getByLabel("Criticality").selectOption("high");
  await expect(page.getByText("Changing Criticality will mark this record Needs confirmation. Prior reviews are kept.")).toBeVisible();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  await expect(page.getByText("High criticality")).toBeVisible();
});

test("Leaving an edited form asks to discard changes; Viewers get no form", async ({ page, browser }) => {
  await signIn(page);
  await page.goto("/applications/new");
  await page.getByLabel("Name (required)").fill("Unsaved draft");
  await page.getByRole("link", { name: "Cancel" }).click();
  const discard = page.getByRole("dialog", { name: "Discard changes?" });
  await discard.getByRole("button", { name: "Keep editing" }).click();
  await expect(page.getByLabel("Name (required)")).toHaveValue("Unsaved draft");
  await page.getByRole("link", { name: "Cancel" }).click();
  await discard.getByRole("button", { name: "Discard changes" }).click();
  await expect(page).toHaveURL(/\/applications$/);

  const viewer = await browser.newPage();
  await signIn(viewer, "viewer@example.test");
  await viewer.goto("/applications/new");
  await expect(viewer.getByRole("heading", { name: "You can't change records" })).toBeVisible();
});
