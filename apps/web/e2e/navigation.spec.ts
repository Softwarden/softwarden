import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Command palette, keyboard shortcuts, the grouped sidebar and its rail. */

test.describe.configure({ mode: "serial", timeout: 180_000 });

const palette = (page: Page) => page.getByRole("dialog", { name: "Command palette" });
const paletteInput = (page: Page) => palette(page).getByRole("combobox", { name: "Search records, pages and actions" });

test("Command palette: search records by kind, open one, recent items, jump to a page, create, cancel on close", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const app = await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active" });
  const other = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  await post(page, "components", { name: `${t} Ledger job`, lifecycle: "active", component_type: "scheduled_job" });
  await post(page, "integrations", { name: `${t} Ledger feed`, lifecycle: "active", source: app.id, target: other.id });
  await post(page, "renewals", { name: `${t} Ledger license`, auto_renew: "unknown" });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();

  // Ctrl+K opens it with the search focused; empty, it offers pages and actions.
  const opener = page.getByRole("button", { name: "Search or jump to…" });
  await opener.focus();
  await page.keyboard.press("Control+k");
  await expect(palette(page)).toBeVisible();
  await expect(paletteInput(page)).toBeFocused();
  await expect(palette(page).getByRole("option", { name: /Renewals/ })).toBeVisible();
  await expect(palette(page).getByRole("option", { name: "Create application" })).toBeVisible();
  await expectNoViolations(page, "Command palette, empty search");

  // Records by kind, from global search (renewals from their list's search).
  await paletteInput(page).fill(`${t} Ledger`);
  const group = (name: string) => palette(page).getByRole("group", { name });
  await expect(group("Applications").getByRole("option", { name: new RegExp(`${t} Ledger`) })).toBeVisible();
  await expect(group("Components").getByRole("option", { name: new RegExp(`${t} Ledger job`) })).toBeVisible();
  await expect(group("Integrations").getByRole("option", { name: new RegExp(`${t} Ledger feed`) })).toBeVisible();
  await expect(group("Renewals").getByRole("option", { name: new RegExp(`${t} Ledger license`) })).toBeVisible();
  await expectNoViolations(page, "Command palette with results");

  // Keyboard: the first record is highlighted; Enter opens it, and focus leaves with the palette.
  await expect(group("Applications").getByRole("option").first()).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/applications/${app.id}$`));
  await expect(palette(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1, name: `${t} Ledger` })).toBeVisible();

  // Recent: the record just opened is first when the palette opens again.
  await page.keyboard.press("Control+k");
  await expect(group("Recent").getByRole("option").first()).toContainText(`${t} Ledger`);

  // Jump to a page.
  await paletteInput(page).fill("renew");
  await expect(group("Pages").getByRole("option", { name: /Renewals/ })).toBeVisible();
  await group("Pages").getByRole("option", { name: /Renewals/ }).click();
  await expect(page).toHaveURL(/\/renewals$/);

  // Create from the palette.
  await page.keyboard.press("Control+k");
  await paletteInput(page).fill("create comp");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/applications\/new\?kind=component$/);

  // Escape closes it and focus returns to the opener.
  await page.goto("/");
  await page.getByRole("button", { name: "Search or jump to…" }).click();
  await expect(palette(page)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(palette(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Search or jump to…" })).toBeFocused();

  // Closing cancels the searches still running.
  const aborted: string[] = [];
  page.on("requestfailed", (r) => { if (r.url().includes("/api/v1/search?q=")) aborted.push(r.failure()?.errorText ?? ""); });
  await page.route(/\/api\/v1\/search\?q=/, async (route) => { await new Promise((done) => setTimeout(done, 4000)); await route.continue().catch(() => undefined); });
  await page.keyboard.press("Control+k");
  await paletteInput(page).fill(`${t} Slow`);
  await page.waitForRequest(/\/api\/v1\/search\?q=/);
  await expect(palette(page).getByText("Searching…")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect.poll(() => aborted.length).toBeGreaterThan(0);
  expect(aborted[0]).toMatch(/ABORTED|cancel/i);
  await page.unroute(/\/api\/v1\/search\?q=/);
});

test("A Viewer's palette has no create actions; theme and shortcuts actions work", async ({ browser, baseURL }) => {
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/");
  await expect(viewer.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  await viewer.keyboard.press("Control+k");
  await expect(palette(viewer)).toBeVisible();
  await expect(palette(viewer).getByRole("option", { name: /^Create/ })).toHaveCount(0);
  await expect(palette(viewer).getByRole("option", { name: "Imports" })).toHaveCount(0); // Editors and Admins only
  await expect(palette(viewer).getByRole("option", { name: /Switch organization/ })).toHaveCount(0); // one organization

  const before = await viewer.evaluate(() => document.documentElement.dataset.theme);
  await paletteInput(viewer).fill("theme");
  await viewer.keyboard.press("Enter");
  await expect.poll(() => viewer.evaluate(() => document.documentElement.dataset.theme)).not.toBe(before);

  await viewer.keyboard.press("Control+k");
  await paletteInput(viewer).fill("shortcuts");
  await viewer.keyboard.press("Enter");
  await expect(viewer.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await expect(viewer.getByRole("dialog", { name: "Keyboard shortcuts" })).not.toContainText("Create a record");

  // `c` does nothing for a Viewer except say why.
  await viewer.keyboard.press("Escape");
  await viewer.goto("/applications");
  await expect(viewer.getByRole("heading", { level: 1, name: "Applications" })).toBeVisible();
  await viewer.keyboard.press("c");
  await expect(viewer.getByTestId("shell-announcer")).toContainText("Viewers can't create records");
  await expect(viewer).toHaveURL(/\/applications$/);
});

test("Keyboard shortcuts: g sequences, / search, c create, ? help; ignored while typing", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.goto("/");
  const h1 = (name: string) => page.getByRole("heading", { level: 1, name });
  await expect(h1("Needs attention")).toBeVisible();

  await page.keyboard.press("g");
  await page.keyboard.press("a");
  await expect(page).toHaveURL(/\/applications$/);
  await expect(h1("Applications")).toBeVisible();
  await page.keyboard.press("g");
  await page.keyboard.press("c");
  await expect(page).toHaveURL(/\/applications\?kind=components$/);
  await expect(h1("Components")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Components" })).toHaveAttribute("aria-current", "page");
  await page.keyboard.press("g");
  await page.keyboard.press("r");
  await expect(h1("Renewals")).toBeVisible();
  await page.keyboard.press("g");
  await page.keyboard.press("n");
  await expect(h1("Needs attention")).toBeVisible();

  // `/` focuses the list's search box; typing there is just typing (no navigation on g a).
  await page.goto("/applications");
  await expect(h1("Applications")).toBeVisible();
  await page.keyboard.press("/");
  const search = page.getByLabel("Search applications");
  await expect(search).toBeFocused();
  await page.keyboard.type("ga");
  await expect(search).toHaveValue("ga");
  await expect(page).toHaveURL(/\/applications/);
  await expect(h1("Applications")).toBeVisible();
  await search.fill("");
  await search.blur();

  // `c` creates a record of the list's kind.
  await page.keyboard.press("c");
  await expect(page).toHaveURL(/\/applications\/new$/);
  await page.goto("/applications?kind=components");
  await expect(h1("Components")).toBeVisible();
  await page.keyboard.press("c");
  await expect(page).toHaveURL(/\/applications\/new\?kind=component$/);
  // Where no list is in context, `c` opens the palette's create actions.
  await page.goto("/");
  await expect(h1("Needs attention")).toBeVisible();
  await page.keyboard.press("c");
  await expect(paletteInput(page)).toHaveValue("Create");
  await expect(palette(page).getByRole("option", { name: "Create renewal" })).toBeVisible();
  await page.keyboard.press("Escape");

  // `/` with no search box on the page opens the palette.
  await page.keyboard.press("/");
  await expect(palette(page)).toBeVisible();
  await page.keyboard.press("Escape");

  // `?` lists every shortcut, focus returns afterwards.
  await page.keyboard.press("?");
  const help = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(help).toBeVisible();
  await expect(help.getByRole("row", { name: /g\s*a\s*Go to Applications/ })).toBeVisible();
  await expect(help.getByRole("row", { name: /g\s*c\s*Go to Components/ })).toBeVisible();
  await expect(help.getByRole("row", { name: /g\s*n\s*Go to Needs attention/ })).toBeVisible();
  await expect(help.getByRole("row", { name: /Ctrl\s*K\s*Open the command palette/ })).toBeVisible();
  await expect(help.getByRole("row", { name: /\[\s*Collapse or expand the sidebar/ })).toBeVisible();
  await expectNoViolations(page, "Keyboard shortcuts help");
  await page.keyboard.press("Escape");
  await expect(help).toHaveCount(0);

  // The help is also in the user menu.
  await page.getByRole("button", { name: /Sam Editor · Editor/ }).click();
  await expectNoViolations(page, "User menu open");
  await page.getByRole("menuitem", { name: "Keyboard shortcuts" }).click();
  await expect(help).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: /Sam Editor · Editor/ })).toBeFocused();
});

test("Grouped sidebar and the collapsed rail ([), kept per user", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const section of ["Work", "Catalog", "Money", "Discovery", "Admin"]) await expect(nav.getByRole("group", { name: section })).toBeVisible();
  await expect(nav.getByRole("group", { name: "Catalog" }).getByRole("link")).toHaveText(["Applications", "Components", "AI agents", "Integrations", "Dependencies", "Data flows", "Vendors", "Vendor risk", "End of life", "People & Teams", "Access review", "AI models", "Software categories", "Tags"]);
  await expect(nav.getByRole("group", { name: "Admin" }).getByRole("link", { name: "Audit log" })).toBeVisible();
  // The organisation name is shown; with one organisation there is no switcher.
  await expect(page.getByRole("button", { name: /Switch organization/ })).toHaveCount(0);
  await expect(page.getByText("Contoso (synthetic)").first()).toBeVisible();
  await expectNoViolations(page, "Grouped sidebar");

  const sidebarWidth = () => nav.evaluate((el) => el.closest(".bg-sidebar")!.getBoundingClientRect().width);
  expect(Math.round(await sidebarWidth())).toBe(232);
  await page.keyboard.press("[");
  await expect.poll(async () => Math.round(await sidebarWidth())).toBe(64);
  await expect(page.getByRole("button", { name: "Collapse sidebar" })).toHaveAttribute("aria-pressed", "true");
  // Links keep their names, and show them as tooltips on hover and focus.
  const renewals = nav.getByRole("link", { name: "Renewals" });
  await expect(renewals).toBeVisible();
  await renewals.hover();
  await expect(page.locator("span.fixed", { hasText: "Renewals" })).toBeVisible();
  await expectNoViolations(page, "Sidebar rail with a tooltip");
  await renewals.click();
  await expect(page.getByRole("heading", { level: 1, name: "Renewals" })).toBeVisible();
  await page.reload();
  await expect.poll(async () => Math.round(await sidebarWidth())).toBe(64);
  await page.keyboard.press("[");
  await expect.poll(async () => Math.round(await sidebarWidth())).toBe(232);

  // A Viewer sees no Admin section and no Imports.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/");
  const vnav = viewer.getByRole("navigation", { name: "Primary" });
  await expect(vnav.getByRole("group", { name: "Catalog" })).toBeVisible();
  await expect(vnav.getByRole("group", { name: "Admin" })).toHaveCount(0);
  await expect(vnav.getByRole("link", { name: "Imports" })).toHaveCount(0);
});

test("Single-key shortcuts can be turned off (WCAG 2.1.4); Ctrl+K still works", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.goto("/profile");
  await expect(page.getByRole("heading", { level: 1, name: "Your profile" })).toBeVisible();
  const toggle = page.getByRole("switch", { name: "Single-key shortcuts" });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await expectNoViolations(page, "Profile with single-key shortcuts off");

  await toggle.blur();
  await page.keyboard.press("g");
  await page.keyboard.press("a");
  await page.keyboard.press("?");
  await page.keyboard.press("c");
  await expect(page).toHaveURL(/\/profile$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // The palette shortcut stays; the help lists only it, and has the switch.
  await page.keyboard.press("Control+k");
  await expect(palette(page)).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /Sam Editor · Editor/ }).click();
  await page.getByRole("menuitem", { name: "Keyboard shortcuts" }).click();
  const help = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(help.getByRole("row", { name: /Open the command palette/ })).toBeVisible();
  await expect(help.getByRole("row", { name: /Go to/ })).toHaveCount(0);
  await help.getByRole("switch", { name: "Single-key shortcuts" }).click();
  await expect(help.getByRole("row", { name: /g\s*a\s*Go to Applications/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press("g");
  await page.keyboard.press("a");
  await expect(page).toHaveURL(/\/applications$/);
});
