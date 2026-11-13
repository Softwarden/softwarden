import { Browser, BrowserContext, expect, Page, test } from "@playwright/test";
import { ANY_REVISION_FOR } from "./helpers";

const PASSWORD = "softwarden-demo-2026"; // synthetic demo accounts from seed_demo

// The login endpoint is throttled (10/min), so each role signs in once and later tests reuse the session.
test.describe.configure({ mode: "serial" }); // one worker, so the cached sessions below are shared

const sessions: Record<string, Promise<Awaited<ReturnType<BrowserContext["storageState"]>>>> = {};

async function pageAs(browser: Browser, baseURL: string | undefined, email: string): Promise<Page> {
  sessions[email] ??= (async () => {
    const page = await browser.newPage({ baseURL });
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    // Every role lands on the Dashboard.
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
    const state = await page.context().storageState();
    await page.close();
    return state;
  })();
  const context = await browser.newContext({ baseURL, storageState: await sessions[email] });
  return context.newPage();
}

/** Synthetic records made through the API with the signed-in session. */
async function post(page: Page, path: string, body: unknown, method = "POST") {
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const res = await page.request.fetch(`/api/v1/${path}`, { method, data: body, headers: { "X-CSRFToken": csrf, ...ANY_REVISION_FOR(method) } });
  expect(res.ok(), await res.text()).toBeTruthy();
  return res.status() === 204 ? null : res.json();
}

async function firstPerson(page: Page): Promise<string> {
  return (await (await page.request.get("/api/v1/people")).json())[0].id;
}

const token = () => `Dt${Date.now()}${Math.floor(Math.random() * 1000)}`;

test("Component name in the list opens the component detail with Ownership and Runtime", async ({ browser, baseURL }) => {
  const name = `${token()} Nightly`;
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await post(page, "components", { name, lifecycle: "active", component_type: "scheduled_job" });
  await page.goto(`/applications?kind=components&q=${encodeURIComponent(name)}`);
  await page.getByRole("region", { name: "Components table" }).getByRole("link", { name }).click();
  await expect(page).toHaveURL(/\/components\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
  await expect(page.getByRole("heading", { name: /^Ownership/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Runtime" })).toBeVisible();
});

test("Application Instances tab shows instances, or the empty state", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const empty = await post(page, "applications", { name: `${t} Empty`, lifecycle: "active" });
  const full = await post(page, "applications", { name: `${t} Full`, lifecycle: "active" });
  await post(page, `applications/${full.id}/instances`, { name: `${t} Production`, environment: "production", status: "degraded" });

  await page.goto(`/applications/${empty.id}?tab=instances`);
  await expect(page.getByText("No instances recorded")).toBeVisible();

  await page.goto(`/applications/${full.id}`);
  await page.getByRole("tab", { name: /^Instances/ }).click();
  await expect(page).toHaveURL(/tab=instances/);
  const table = page.getByRole("region", { name: "Instances table" });
  await expect(table.getByRole("rowheader", { name: `${t} Production` })).toBeVisible();
  await expect(table.getByText("Degraded")).toBeVisible();
});

test("Editor archives an application; Restore clears the banner and shows the success note", async ({ browser, baseURL }) => {
  const name = `${token()} Archivable`;
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const app = await post(page, "applications", { name, lifecycle: "active" });
  await page.goto(`/applications/${app.id}`);

  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Archive" }).click();
  await page.getByRole("dialog", { name: `Archive ${name}?` }).getByRole("button", { name: "Archive", exact: true }).click();
  const banner = page.getByRole("region", { name: "Archived record" });
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(/Archived on \d{4}-\d{2}-\d{2} by /);

  await banner.getByRole("button", { name: "Restore" }).click();
  await expect(banner).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "Restored." })).toBeAttached();
  await expect(page.getByRole("heading", { level: 1, name })).toBeFocused();
});

test("Admin delete: a record with a review is refused; an unreferenced one is deleted", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const reviewer = await firstPerson(page);
  const reviewed = await post(page, "applications", { name: `${t} Reviewed`, lifecycle: "active" });
  await post(page, `applications/${reviewed.id}/reviews`, { confirmed_by: reviewer, evidence: "Synthetic email" });
  const loose = await post(page, "components", { name: `${t} Loose`, lifecycle: "active", component_type: "scheduled_job" });

  await page.goto(`/applications/${reviewed.id}`);
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const refused = page.getByRole("dialog", { name: `Delete ${t} Reviewed?` });
  await refused.getByLabel("Name (required)").fill(`${t} Reviewed`);
  await refused.getByRole("button", { name: `Delete ${t} Reviewed` }).click();
  await expect(refused.getByRole("alert")).toHaveText(`${t} Reviewed can't be deleted because it has 1 review. Archive it instead.`);
  await expect(refused.getByRole("button", { name: "Archive instead" })).toBeVisible();
  expect((await page.request.get(`/api/v1/applications/${reviewed.id}`)).status()).toBe(200);

  await page.goto(`/components/${loose.id}`);
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const confirm = page.getByRole("dialog", { name: `Delete ${t} Loose?` });
  await confirm.getByLabel("Name (required)").fill(`${t} Loose`);
  await confirm.getByRole("button", { name: `Delete ${t} Loose` }).click();
  await expect(page).toHaveURL(/\/applications\?kind=components$/);
  await expect(page.getByRole("heading", { level: 1, name: "Components" })).toBeVisible();
  expect((await page.request.get(`/api/v1/components/${loose.id}`)).status()).toBe(404);
});

test("Viewer sees the detail but none of the write actions", async ({ browser, baseURL }) => {
  const name = `${token()} ReadOnly`;
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const comp = await post(page, "components", { name, lifecycle: "active", component_type: "scheduled_job" });

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/components/${comp.id}`);
  await expect(viewer.getByRole("heading", { level: 1, name })).toBeVisible();
  await expect(viewer.getByRole("heading", { name: "Runtime" })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Record review" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: "Edit ownership" })).toHaveCount(0);
  await viewer.getByRole("button", { name: "More" }).click();
  await expect(viewer.getByRole("menuitem", { name: "Copy Softwarden ID" })).toBeVisible();
  await expect(viewer.getByRole("menuitem", { name: "Archive" })).toHaveCount(0);
  await expect(viewer.getByRole("menuitem", { name: "Delete" })).toHaveCount(0);
});

test("Editor assigns owners and records a review on a component", async ({ browser, baseURL }) => {
  const name = `${token()} Reviewable`;
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const comp = await post(page, "components", { name, lifecycle: "active", component_type: "scheduled_job" });
  await page.goto(`/components/${comp.id}`);

  await page.getByRole("button", { name: "Edit ownership" }).click();
  const owners = page.getByRole("dialog", { name: `Assign owners — ${name}` });
  await owners.getByLabel("Criticality").selectOption("high");
  await owners.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Criticality set to High." })).toBeAttached();

  await page.getByRole("button", { name: "Record review" }).click();
  const review = page.getByRole("dialog", { name: `Record review — ${name}` });
  await review.getByLabel("Confirmed by (required)").selectOption({ index: 1 });
  await review.getByLabel("Evidence / reference (required)").fill("Synthetic email");
  await review.getByRole("button", { name: "Record review" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Review recorded." })).toBeAttached();
  await expect(page.getByRole("tab", { name: "Review history (1)" })).toBeVisible();
  await page.getByRole("tab", { name: /^Review history/ }).click();
  await expect(page.getByRole("cell", { name: "Synthetic email" })).toBeVisible();
});

test("Undo restores the previous owner, and is refused once another editor changed it", async ({ browser, baseURL }) => {
  const name = `${token()} Undoable`;
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const app = await post(page, "applications", { name, lifecycle: "active" });
  const people = (await (await page.request.get("/api/v1/people?active=true")).json()) as { id: string; name: string }[];
  const priya = people.find((p) => p.name === "Priya Raman")!;
  await page.goto(`/applications/${app.id}`);

  const assign = async (person: string) => {
    await page.getByRole("button", { name: "Edit ownership" }).click();
    const owners = page.getByRole("dialog", { name: `Assign owners — ${name}` });
    await owners.getByRole("combobox", { name: "Technical owner" }).fill(person);
    await page.getByRole("option", { name: new RegExp(`^${person}`) }).first().click();
    await owners.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("status").filter({ hasText: `Technical owner set to ${person}.` })).toBeVisible();
  };

  // Plain Undo: the gap comes back.
  await assign("Dana Ortiz");
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Undone." })).toBeAttached();
  await expect(page.locator("dl.ownership").getByText("Dana Ortiz")).toHaveCount(0);

  // Another editor changes the owner before Undo: Undo is refused and the newer owner stays.
  await assign("Dana Ortiz");
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await post(admin, `applications/${app.id}`, { technical_owner: priya.id }, "PATCH");
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByRole("alert")).toContainText("Not undone: Alex Admin changed Technical owner at");
  await expect(page.locator("dl.ownership").getByText("Priya Raman")).toBeVisible();
});

test("Editor adds, renames and deletes an instance; a duplicate name is a blocking field error", async ({ browser, baseURL }) => {
  const name = `${token()} Instanced`;
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const app = await post(page, "applications", { name, lifecycle: "active" });
  await page.goto(`/applications/${app.id}?tab=instances`);

  await page.getByRole("button", { name: "Add instance" }).click();
  const add = page.getByRole("dialog", { name: `Add instance — ${name}` });
  await add.getByLabel("Name (required)").fill("Production");
  await add.getByLabel("Environment").fill("Production");
  await add.getByLabel("Status").selectOption("operational");
  await add.getByRole("button", { name: "Add instance" }).click();
  await expect(page.getByRole("tab", { name: "Instances (1)" })).toBeVisible();

  await page.getByRole("button", { name: "Add instance" }).click();
  await add.getByLabel("Name (required)").fill("production");
  await add.getByRole("button", { name: "Add instance" }).click();
  await expect(add.getByRole("alert")).toContainText(`An instance named "production" already exists for ${name}.`);
  await add.getByLabel("Name (required)").fill("Sandbox");
  await add.getByRole("button", { name: "Add instance" }).click();
  await expect(page.getByRole("tab", { name: "Instances (2)" })).toBeVisible();

  await page.getByRole("button", { name: "Edit Sandbox" }).click();
  const edit = page.getByRole("dialog", { name: "Edit instance — Sandbox" });
  await edit.getByLabel("Name (required)").fill("UAT");
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("rowheader", { name: "UAT" })).toBeVisible();

  await page.getByRole("button", { name: "Delete UAT" }).click();
  await page.getByRole("dialog", { name: "Delete UAT?" }).getByRole("button", { name: "Delete UAT" }).click();
  await expect(page.getByRole("tab", { name: "Instances (1)" })).toBeVisible();
  await expect(page.getByRole("rowheader", { name: "UAT" })).toHaveCount(0);
});

test("Audit activity shows changes to any role; the Admin audit log filters by event", async ({ browser, baseURL }) => {
  const name = `${token()} Audited`;
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const app = await post(editor, "applications", { name, lifecycle: "active" });
  await post(editor, `applications/${app.id}`, { criticality: "high", purpose: "Synthetic purpose" }, "PATCH");

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${app.id}`);
  await viewer.getByRole("tab", { name: "Audit activity" }).click();
  const activity = viewer.getByRole("region", { name: "Audit activity table" });
  await expect(activity.getByRole("row").nth(1)).toContainText("Criticality: not set → high");
  await expect(activity).toContainText("Record created");

  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/admin/audit");
  await admin.getByLabel("Event").selectOption({ label: "Record changes" });
  const log = admin.getByRole("region", { name: "Audit log table" });
  await expect(log.getByRole("link", { name }).first()).toBeVisible();
  await admin.getByLabel("Event").selectOption({ label: "Sign-ins" });
  await expect(log).toContainText("Sign-in");
  await expect(viewer.getByRole("link", { name: "Audit log" })).toHaveCount(0);
});
