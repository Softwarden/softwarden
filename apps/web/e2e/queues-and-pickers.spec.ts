import { expect, test, type Page } from "@playwright/test";
import { pageAs, PASSWORD, personId, post, token, showAllClear } from "./helpers";

/** Needs attention's scopes, row actions and sections, the pickers, the sign-in return address and the explorer's
 *  focus, each with its acceptance scenario. */

test.describe.configure({ mode: "serial" });

const section = (page: Page, name: string) => page.getByRole("region", { name: new RegExp(`^${name}`) });

test("Signing in returns to the whole shared URL, query and fragment included", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const core = await post(editor, "applications", { name: `${t} Core`, lifecycle: "active" });
  const page = await browser.newPage({ baseURL });
  await page.goto(`/dependencies?target=${core.id}&depth=all&view=table#results`);
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel("Email").fill("editor@example.test");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(new RegExp(`/dependencies\\?target=${core.id}&depth=all&view=table#results$`));
  await expect(page.getByRole("combobox", { name: /target/i })).toHaveValue(`${t} Core`);
  await expect(page.getByLabel("Depth")).toHaveValue("all");
  await page.close();

  const second = await browser.newPage({ baseURL });
  await second.goto(`/applications/${core.id}?tab=relationships`);
  await second.getByLabel("Email").fill("editor@example.test");
  await second.getByLabel("Password").fill(PASSWORD);
  await second.getByRole("button", { name: "Sign in" }).click();
  await expect(second).toHaveURL(new RegExp(`/applications/${core.id}\\?tab=relationships$`));
  await expect(second.getByRole("tab", { name: /Relationships/ })).toHaveAttribute("aria-selected", "true");
  await second.close();
});

test("Arrows while results are still loading, then Enter picks the announced option", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const app = await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active" });
  for (const name of ["Alpha", "Beta", "Gamma"]) await post(page, "applications", { name: `${t} ${name}`, lifecycle: "active" });
  let release: () => void = () => undefined;
  const held = new Promise<void>((r) => { release = r; });
  await page.route(`**/api/v1/applications?page_size=8&q=${t}*`, async (route) => { await held; await route.continue(); });
  await page.route(`**/api/v1/components?page_size=8&q=${t}*`, async (route) => { await held; await route.continue(); });
  await page.goto(`/applications/${app.id}?tab=relationships`);
  await page.getByRole("button", { name: "Add relationship" }).click();
  const box = page.getByRole("combobox", { name: "Other record (required)" });
  await box.fill(t);
  await box.press("ArrowDown"); // nothing to move to yet
  await box.press("ArrowDown");
  release();
  await expect(page.getByRole("option", { name: new RegExp(`${t} Alpha`) })).toBeVisible();
  await box.press("ArrowDown");
  const active = await box.getAttribute("aria-activedescendant");
  expect(active).toBeTruthy();
  const announced = await page.locator(`[id="${active}"]`).innerText();
  await expect(page.locator(`[id="${active}"]`)).toHaveClass(/bg-accent/);
  await box.press("Enter");
  await expect(box).toHaveValue(announced.split("\n")[0]);
  // The list changes under the highlight: the announced, highlighted and chosen option stay one and the same.
  const list = page.getByRole("listbox", { name: "Record suggestions" });
  await box.fill(`${t} Gamma`);
  await expect(list.getByRole("option", { name: new RegExp(`${t} Gamma`) })).toBeVisible();
  await box.press("End");
  await box.press("ArrowDown"); // wraps to the first
  const now = await box.getAttribute("aria-activedescendant");
  const highlighted = list.locator(".bg-accent");
  await expect(highlighted).toHaveCount(1);
  expect(await highlighted.getAttribute("id")).toBe(now);
  const name = (await highlighted.innerText()).split("\n")[0];
  await box.press("Enter");
  await expect(box).toHaveValue(name);
});

test("A failed instance lookup offers Retry or a deliberate whole-application choice", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const app = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const other = await post(page, "applications", { name: `${t} HR`, lifecycle: "active" });
  for (const name of ["Production", "Sandbox"]) await post(page, `applications/${other.id}/instances`, { name });
  let failing = true;
  await page.route(`**/api/v1/applications/${other.id}/instances`, (route) =>
    failing ? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Server error." }) }) : route.continue());
  await page.goto(`/applications/${app.id}?tab=relationships`);
  await page.getByRole("button", { name: "Add relationship" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} HR`);
  await page.getByRole("option", { name: new RegExp(`${t} HR`) }).click();
  await dialog.getByLabel("Purpose").fill("Reads employee records");
  await expect(dialog.getByRole("alert").filter({ hasText: `Couldn't load the deployments of ${t} HR` })).toBeVisible();
  await dialog.getByRole("button", { name: "Add relationship" }).click();
  await expect(dialog.getByRole("alert").filter({ hasText: "Fix this field" })).toContainText("Retry, or choose to link the application as a whole");
  failing = false;
  await dialog.getByRole("button", { name: "Retry" }).click();
  await dialog.getByLabel(`Instance of ${t} HR (optional)`).selectOption({ label: "Sandbox" });
  await expect(dialog.getByRole("combobox", { name: "Other record (required)" })).toHaveValue(`${t} HR`);
  await expect(dialog.getByLabel("Purpose")).toHaveValue("Reads employee records");
  await dialog.getByRole("button", { name: "Add relationship" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("tabpanel")).toContainText("Sandbox");

  // The whole-application choice, made on purpose.
  failing = true;
  const third = await post(page, "applications", { name: `${t} Third`, lifecycle: "active" });
  await page.goto(`/applications/${third.id}?tab=relationships`);
  await page.getByRole("button", { name: "Add relationship" }).click();
  await page.getByRole("dialog").getByRole("combobox", { name: "Other record (required)" }).fill(`${t} HR`);
  await page.getByRole("option", { name: new RegExp(`${t} HR`) }).click();
  await page.getByRole("dialog").getByLabel(`Link ${t} HR as a whole (no particular deployment)`).check();
  await page.getByRole("dialog").getByRole("button", { name: "Add relationship" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("Rows follow the selected scope through loading and failure; empty text fits the scope", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  // An overdue review belonging to another steward (the admin), and nothing of the editor's own in reviews.
  const admin = await (await (await pageAs(browser, baseURL, "admin@example.test")).request.get("/api/v1/auth/me")).json();
  const app = await post(editor, "applications", { name: `${t} Theirs`, lifecycle: "active", it_steward: admin.user.id });
  await post(editor, `applications/${app.id}/reviews`, {
    confirmed_by: await personId(editor, "Dana Ortiz"), evidence: "Email", confirmation_date: "2025-01-01", next_review_date: "2025-04-01",
  });
  const real = (await (await editor.request.get("/api/v1/needs-attention?scope=mine")).json());
  const mineBody = JSON.stringify({ ...real, reviews: [] });

  let mode: "hold" | "fail" | "ok" = "hold";
  let release: () => void = () => undefined;
  await editor.route("**/api/v1/needs-attention?scope=mine", async (route) => {
    if (mode === "hold") { await new Promise<void>((r) => { release = r; }); return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Couldn't load." }) }); }
    if (mode === "fail") return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Couldn't load." }) });
    return route.fulfill({ contentType: "application/json", body: mineBody });
  });
  await editor.goto("/?scope=all");
  await expect(section(editor, "Reviews").getByRole("row", { name: new RegExp(`${t} Theirs`) })).toBeVisible();
  await editor.getByRole("button", { name: /Assigned to me/ }).click();
  // Loading: the All rows and their actions are gone at once.
  await expect(editor.getByText("Loading Needs attention…")).toBeVisible();
  await expect(editor.getByRole("row", { name: new RegExp(`${t} Theirs`) })).toHaveCount(0);
  release();
  await expect(editor.getByRole("alert").filter({ hasText: "Couldn't load." })).toBeVisible();
  await expect(editor.getByRole("row", { name: new RegExp(`${t} Theirs`) })).toHaveCount(0);
  mode = "ok";
  await editor.getByRole("button", { name: "Retry" }).click();
  await showAllClear(editor);
  await expect(section(editor, "Reviews")).toContainText("No reviews due for records assigned to you.");
  await expect(editor.getByText("Showing", { exact: false }).filter({ hasText: "assigned to you" })).toHaveCount(1);
  await expect(section(editor, "Reviews")).not.toContainText("Every active record");
  // All still lists it, and Unassigned has its own wording.
  await editor.getByRole("button", { name: /^All/ }).click();
  await expect(section(editor, "Reviews").getByRole("row", { name: new RegExp(`${t} Theirs`) })).toBeVisible();
  await editor.unroute("**/api/v1/needs-attention?scope=mine");
  await editor.route("**/api/v1/needs-attention?scope=unassigned", async (route) => {
    const body = await (await route.fetch()).json();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...body, reviews: [] }) });
  });
  await editor.getByRole("button", { name: /^Unassigned/ }).click();
  await expect(section(editor, "Reviews")).toContainText("No reviews due for records without an IT steward.");
});

test("A late response for an earlier scope never replaces the current one", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  let releaseMine: () => void = () => undefined;
  await editor.route("**/api/v1/needs-attention?scope=mine", async (route) => {
    await new Promise<void>((r) => { releaseMine = r; });
    const body = await (await route.fetch()).json();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...body, scope: "mine" }) });
  });
  await editor.goto("/?scope=all");
  await expect(editor.getByRole("heading", { name: /Ownership gaps/ })).toBeVisible();
  await editor.getByRole("button", { name: /Assigned to me/ }).click();
  await editor.getByRole("button", { name: /^Unassigned/ }).click();
  await expect(editor.getByText("with no steward or owner", { exact: false })).toHaveCount(1);
  const lateMine = editor.waitForResponse((r) => r.url().includes("/api/v1/needs-attention?scope=mine"));
  releaseMine();
  await (await lateMine).finished(); // the late answer has fully reached the page; two frames let it render it
  await editor.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  await expect(editor).toHaveURL(/scope=unassigned/);
  await expect(editor.getByText("with no steward or owner", { exact: false })).toHaveCount(1);
});

test("Record review and Assign owner open the record's dialog from the queue", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const me = await (await editor.request.get("/api/v1/auth/me")).json();
  const gap = await post(editor, "applications", { name: `${t} Gappy`, lifecycle: "active", it_steward: me.user.id });
  await post(editor, "applications", { name: `${t} Gappy 2`, lifecycle: "active", it_steward: me.user.id });
  await editor.goto("/?scope=all");
  const owners = section(editor, "Ownership gaps");
  // Cancel leaves the queue as it was and returns focus to the button.
  const assign = owners.getByRole("button", { name: `Assign owner for ${t} Gappy`, exact: true });
  await assign.click();
  await expect(editor.getByRole("dialog", { name: `Assign owners — ${t} Gappy` })).toBeVisible();
  await editor.keyboard.press("Escape");
  await expect(assign).toBeFocused();
  await expect(editor).toHaveURL(/scope=all/);
  // Save: the scope stays, and when the row leaves, focus moves to the next row's action.
  await assign.click();
  const dialog = editor.getByRole("dialog");
  for (const [field, value] of [["Business owner", "Priya Raman"], ["Technical owner", "Dana Ortiz"]] as const) {
    await dialog.getByRole("combobox", { name: field }).fill(value);
    await editor.getByRole("option", { name: new RegExp(value) }).first().click();
  }
  await dialog.getByRole("combobox", { name: "Support team" }).fill("IT Applications");
  await editor.getByRole("option", { name: /IT Applications/ }).first().click();
  await dialog.getByRole("button", { name: /Save/ }).click();
  await expect(owners.getByRole("row", { name: new RegExp(`${t} Gappy\\b(?! 2)`) })).toHaveCount(0);
  await expect(editor).toHaveURL(/scope=all/);
  await expect(editor.locator(":focus")).toHaveAttribute("data-queue-action", "");
  // Record review, one activation; the record-name link still opens the overview.
  const reviews = section(editor, "Reviews");
  await reviews.getByRole("button", { name: `Record review for ${t} Gappy`, exact: true }).click();
  await expect(editor.getByRole("dialog")).toBeVisible();
  await editor.keyboard.press("Escape");
  await reviews.getByRole("link", { name: `${t} Gappy`, exact: true }).click();
  await expect(editor).toHaveURL(new RegExp(`/applications/${gap.id}$`));
  // A Viewer keeps View record.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/?scope=all");
  await expect(section(viewer, "Reviews").getByRole("link", { name: `View record for ${t} Gappy`, exact: true })).toBeVisible();
  await expect(section(viewer, "Reviews").getByRole("button", { name: /Record review/ })).toHaveCount(0);
});

test("Queue sections collapse by keyboard at 390 px, and a jump link opens and focuses one", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await post(editor, "applications", { name: `${token()} Gap`, lifecycle: "active" });
  await editor.setViewportSize({ width: 390, height: 844 });
  await editor.goto("/?scope=all");
  const toggle = editor.getByRole("button", { name: /^Ownership gaps \(\d+\)$/ });
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await toggle.focus();
  await editor.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(section(editor, "Ownership gaps").getByRole("table")).toBeHidden();
  await editor.getByRole("navigation", { name: "Queues" }).getByRole("link", { name: /Ownership gaps/ }).click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(editor.getByRole("heading", { name: /Ownership gaps/ })).toBeFocused();
  // An empty queue is one line, without a disclosure.
  await editor.route("**/api/v1/needs-attention?scope=all", async (route) => {
    const body = await (await route.fetch()).json();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...body, stale_assessments: [] }) });
  });
  await editor.reload();
  await showAllClear(editor);
  await expect(section(editor, "Stale assessments")).toContainText("No stale assessments.");
  await expect(section(editor, "Stale assessments").getByRole("button", { name: /Stale assessments/ })).toHaveCount(0);
});

test("Show path moves focus to the selected path, and Back returns to the row", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const core = await post(editor, "applications", { name: `${t} Core`, lifecycle: "active" });
  for (let i = 0; i < 12; i++) {
    const r = await post(editor, "applications", { name: `${t} Dep ${String(i).padStart(2, "0")}`, lifecycle: "active" });
    await post(editor, "relationships", { type: "depends_on", source: r.id, target: core.id });
  }
  for (const width of [1280, 1700]) { // panel below the table, then beside it
    await editor.setViewportSize({ width, height: 800 });
    await editor.goto(`/dependencies?target=${core.id}&view=table`);
    const show = editor.getByRole("button", { name: `Show path for ${t} Dep 06` });
    await show.focus();
    await editor.keyboard.press("Enter");
    const heading = editor.getByRole("heading", { name: `Selected: ${t} Dep 06` });
    await expect(heading).toBeFocused();
    await editor.keyboard.press("Tab");
    await expect(editor.locator(":focus")).toHaveText(/Open record/);
    await editor.getByRole("button", { name: /Back to the selected row/ }).click();
    await expect(show).toBeFocused();
  }
});
