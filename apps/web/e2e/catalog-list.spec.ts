import { expect, Page, test } from "@playwright/test";
import { ANY_REVISION_FOR } from "./helpers";

const EDITOR = { email: "editor@example.test", password: "softwarden-demo-2026" };

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EDITOR.email);
  await page.getByLabel("Password").fill(EDITOR.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible(); // everyone's home
}

/** Synthetic records made through the API with the signed-in session. */
async function post(page: Page, path: string, body: unknown, method = "POST") {
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const res = await page.request.fetch(`/api/v1/${path}`, { method, data: body, headers: { "X-CSRFToken": csrf, ...ANY_REVISION_FOR(method) } });
  expect(res.ok(), await res.text()).toBeTruthy();
  return res.json();
}

test("Applications and components list: search, Missing owners, kind switch, URL state", async ({ page }) => {
  const token = `Zq${Date.now()}`;
  await signIn(page);
  const people = await (await page.request.get("/api/v1/people")).json();
  const teams = await (await page.request.get("/api/v1/teams")).json();
  const bare = await post(page, "applications", { name: `${token} Bare`, lifecycle: "active" });
  const owned = await post(page, "applications", { name: `${token} Owned`, lifecycle: "active" });
  await post(page, `applications/${owned.id}`, {
    business_owner: people[0].id, technical_owner: people[0].id, support_team: teams[0].id, criticality: "high",
  }, "PATCH");
  await post(page, "components", { name: `${token} Nightly`, lifecycle: "active", component_type: "scheduled_job" });
  expect(bare.id).toBeTruthy();
  await post(page, "applications", { name: `${token} Retired`, lifecycle: "retired" });

  const table = page.getByRole("region", { name: "Applications table" });
  await page.getByRole("link", { name: "Applications", exact: true }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Applications" })).toBeVisible();

  // Search narrows the list.
  await page.getByLabel("Search applications").fill(token);
  await expect(page).toHaveURL(new RegExp(`q=${token}`));
  await expect(table.getByRole("link", { name: `${token} Bare` })).toBeVisible();
  await expect(table.getByRole("link", { name: `${token} Owned` })).toBeVisible();
  // The default Active view excludes retired records.
  await expect(table.getByRole("link", { name: `${token} Retired` })).toHaveCount(0);

  // Missing owners keeps only the bare record, and shows its gap text.
  await page.getByRole("button", { name: "Missing owners" }).click();
  await expect(page).toHaveURL(/preset=missing-owners/);
  await expect(table.getByRole("link", { name: `${token} Owned` })).toHaveCount(0);
  await expect(table.getByRole("link", { name: `${token} Bare` })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${token} Bare.*No business owner`) })).toBeVisible();
  await expect(page.getByRole("button", { name: "Missing owners" })).toHaveAttribute("aria-pressed", "true");

  // State survives a reload.
  await page.reload();
  await expect(page.getByLabel("Search applications")).toHaveValue(token);
  await expect(page.getByRole("button", { name: "Missing owners" })).toHaveAttribute("aria-pressed", "true");
  await expect(table.getByRole("link", { name: `${token} Bare` })).toBeVisible();
  await expect(table.getByRole("link", { name: `${token} Owned` })).toHaveCount(0);

  // No results offers Clear filters.
  await page.getByRole("button", { name: "Overdue reviews" }).click();
  await expect(page.getByText("No applications match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).not.toHaveURL(/preset=|q=/);
  await expect(page.getByLabel("Search applications")).toHaveValue("");

  // Switching to Components keeps the search out of the way and shows the type column.
  await page.getByLabel("Search applications").fill(token);
  // The sidebar's Components entry; the page has no Kind switch of its own.
  await expect(page.getByRole("navigation", { name: "Kind" })).toHaveCount(0);
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Components", exact: true }).click();
  await expect(page).toHaveURL(/kind=components/);
  await expect(page.getByRole("heading", { level: 1, name: "Components" })).toBeVisible();
  await expect(page.getByLabel("Search components")).toBeVisible();
  await page.getByLabel("Search components").fill(token);
  const comps = page.getByRole("region", { name: "Components table" });
  await expect(comps.getByRole("row", { name: new RegExp(`${token} Nightly.*Scheduled job`) })).toBeVisible();
  // Component names link to the component detail screen.
  await expect(comps.getByRole("link", { name: `${token} Nightly` })).toHaveAttribute("href", /^\/components\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("link", { name: "Add application" })).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`kind=components.*q=${token}|q=${token}.*kind=components`));
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Components" })).toBeVisible();
  await expect(page.getByLabel("Search components")).toHaveValue(token);
  await expect(comps.getByText(`${token} Nightly`)).toBeVisible();
});
