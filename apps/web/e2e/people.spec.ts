import { expect, test, type Page } from "@playwright/test";
import { ANY_REVISION_FOR } from "./helpers";

const PASSWORD = "softwarden-demo-2026"; // synthetic demo accounts from seed_demo

async function signIn(page: Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  // Every role lands on the Dashboard.
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
}

/** Call the API with the page's session, as the SPA does (CSRF header from the cookie). */
async function apiCall(page: Page, method: string, path: string, data?: unknown) {
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const response = await page.request.fetch(`/api/v1/${path}`, { method, data, headers: { "X-CSRFToken": csrf, ...ANY_REVISION_FOR(method) } });
  return response.json().catch(() => null);
}

test("Editor adds a person; a duplicate email names the existing person; marking inactive warns about owned records", async ({ page }) => {
  const name = `Pat Lee ${Date.now()}`;
  await signIn(page, "editor@example.test");
  await page.getByRole("link", { name: "People & Teams" }).click();

  await page.getByRole("button", { name: "Add person" }).click();
  const add = page.getByRole("dialog", { name: "Add person" });
  await add.getByLabel("Name (required)").fill(name);
  await add.getByLabel("Email").fill("DANA.ORTIZ@example.test");
  await add.getByRole("button", { name: "Save" }).click();
  await expect(add.getByRole("alert")).toContainText("A person with this email already exists: Dana Ortiz.");
  await add.getByLabel("Email").fill(`pat.${Date.now()}@example.test`);
  await add.getByLabel("Title").fill("Payroll lead");
  await add.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").filter({ hasText: `${name} added.` })).toBeAttached();

  // Make them the business owner of a new application, then mark them inactive.
  const people = (await apiCall(page, "GET", "people")) as { id: string; name: string }[];
  const pat = people.find((p) => p.name === name)!;
  await apiCall(page, "POST", "applications", { name: `Owned by ${name}`, lifecycle: "active", business_owner: pat.id });

  await page.getByLabel("Search").fill(name);
  await page.getByRole("button", { name, exact: true }).click();
  const panel = page.getByRole("dialog", { name });
  await expect(panel.getByRole("link", { name: `Owned by ${name}` })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Anonymize person" })).toHaveCount(0); // Admin only
  await panel.getByRole("button", { name: "Mark inactive" }).click();
  const confirm = page.getByRole("dialog", { name: `Mark ${name} inactive?` });
  await expect(confirm).toContainText(`${name} owns 1 record. Marking inactive creates ownership gaps`);
  await confirm.getByRole("button", { name: "Mark inactive" }).click();
  await expect(page.getByRole("row", { name: new RegExp(name) })).toContainText("Inactive");
});

test("Admin anonymizes a person only after typing their name", async ({ page }) => {
  const name = `Leaver ${Date.now()}`;
  await signIn(page, "admin@example.test");
  await apiCall(page, "POST", "people", { name, email: `leaver.${Date.now()}@example.test` });
  await page.goto("/admin/people");
  await page.getByLabel("Search").fill(name);
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("dialog", { name }).getByRole("button", { name: "Anonymize person" }).click();

  const dialog = page.getByRole("dialog", { name: `Anonymize ${name}` });
  const go = dialog.getByRole("button", { name: `Anonymize ${name}` });
  await expect(go).toBeDisabled();
  await dialog.getByLabel("Type the person's name to confirm").fill(name.toLowerCase());
  await expect(go).toBeDisabled();
  await dialog.getByLabel("Type the person's name to confirm").fill(name);
  await go.click();
  await expect(page.getByRole("status").filter({ hasText: `${name} was anonymized.` })).toBeAttached();
  await page.getByLabel("Search").fill("");
  await expect(page.getByRole("rowheader", { name: new RegExp(name) })).toHaveCount(0);
});

test("Teams show description and use; delete is offered only when unused and a refusal names what uses it", async ({ page }) => {
  const team = `Team ${Date.now()}`;
  await signIn(page, "editor@example.test");
  await page.goto("/admin/people?tab=teams");
  await page.getByRole("button", { name: "Add team" }).click();
  const add = page.getByRole("dialog", { name: "Add team" });
  await add.getByLabel("Name (required)").fill(team);
  await add.getByLabel("Description").fill("Middleware and scheduled jobs");
  await add.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("row", { name: new RegExp(team) })).toContainText("Middleware and scheduled jobs");
  await expect(page.getByText("A team can be a support team, never an owner.")).toBeVisible();

  // Open the delete dialog, then let another editor start using the team before confirming.
  await page.getByRole("button", { name: `Delete ${team}` }).click();
  const teams = (await apiCall(page, "GET", "teams")) as { id: string; name: string }[];
  const id = teams.find((t) => t.name === team)!.id;
  await apiCall(page, "POST", "applications", { name: `Supported by ${team}`, lifecycle: "active", support_team: id });
  const confirm = page.getByRole("dialog", { name: `Delete ${team}?` });
  await confirm.getByRole("button", { name: `Delete ${team}` }).click();
  await expect(confirm.getByRole("alert")).toHaveText(
    `${team} can't be deleted: 1 record uses it (Supported by ${team}). Reassign them first.`,
  );
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await page.reload();
  await expect(page.getByRole("row", { name: new RegExp(team) })).toContainText("1 record");
  await expect(page.getByRole("button", { name: `Delete ${team}` })).toHaveCount(0);
});

test("Viewer sees People & Teams read-only", async ({ page }) => {
  await signIn(page, "viewer@example.test");
  await page.goto("/admin/people?tab=vendors");
  await expect(page.getByRole("region", { name: "Vendors table" })).toContainText("Used by");
  await expect(page.getByRole("button", { name: /^(Add|Edit|Delete)/ })).toHaveCount(0);
});
