import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PASSWORD, pageAs, post, token } from "./helpers";

/** The organisation switcher at the top of the sidebar (multi-organisation membership). The test-only
 *  `e2e/multi-org-user` route gives `multi@example.test` an Editor membership in Contoso and in a second organisation,
 *  Fabrikam. Switching calls POST auth/me/organization, reloads `me` (which clears every cached query) and lands on the
 *  Dashboard (everyone's home); only the current organisation's records show. */

test.describe.configure({ mode: "serial", timeout: 180_000 });

async function signIn(page: Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(page).toHaveURL(/\/dashboard$/);
}

test("A user in two organizations switches; cached lists, palette results and other tabs follow without a reload", async ({ browser, baseURL }) => {
  const t = token();
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await post(admin, "e2e/multi-org-user", {});

  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await signIn(page, "multi@example.test");
  const switcher = page.getByRole("button", { name: /Organization\s*:\s*Contoso \(synthetic\)\s*\.\s*Switch organization/ });
  await expect(switcher).toBeVisible();
  await post(page, "applications", { name: `${t} In Contoso`, lifecycle: "active" });
  const table = (p: Page) => p.getByRole("region", { name: "Applications table" });
  const contosoRow = (p: Page) => table(p).getByRole("link", { name: `${t} In Contoso` });

  // Contoso's record is in the list (and so in the query cache) in this tab and in a second tab.
  await page.goto(`/applications?q=${t}`);
  await expect(contosoRow(page)).toBeVisible();
  const other = await context.newPage();
  await other.goto(`/applications?q=${t}`);
  await expect(contosoRow(other)).toBeVisible();
  // And in a palette search, which is cached too.
  await page.keyboard.press("Control+k");
  const paletteSearch = page.getByRole("combobox", { name: "Search records, pages and actions" });
  await paletteSearch.fill(`${t} In`);
  await expect(page.getByRole("option", { name: new RegExp(`${t} In Contoso`) })).toBeVisible();
  await page.keyboard.press("Escape");

  // Switch to Fabrikam from the sidebar: the Dashboard, its heading focused, announced.
  await switcher.click();
  const menu = page.getByRole("menu", { name: "Switch organization" });
  await expect(menu.getByRole("menuitemradio", { name: "Contoso (synthetic) (Editor)" })).toHaveAttribute("aria-checked", "true");
  await expectNoViolations(page, "Organization switcher open");
  await menu.getByRole("menuitemradio", { name: "Fabrikam (synthetic) (Editor)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeFocused();
  await expect(page.getByRole("button", { name: /Organization\s*:\s*Fabrikam \(synthetic\)\s*\.\s*Switch organization/ })).toBeVisible();
  await expect(page.locator("header")).toContainText("Fabrikam (synthetic)");
  await expect(page.getByTestId("shell-announcer")).toHaveText("Switched to Fabrikam (synthetic).");
  await expectNoViolations(page, "Dashboard after switching organization");

  // Back to the list in the app (no reload): the cached Contoso rows are gone.
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/applications\\?q=${t}`));
  await expect(page.getByText("No applications match these filters")).toBeVisible();
  await expect(contosoRow(page)).toHaveCount(0);
  // The same palette search no longer finds it either.
  await page.keyboard.press("Control+k");
  await paletteSearch.fill(`${t} In`);
  await expect(page.getByRole("dialog", { name: "Command palette" }).getByText("Nothing matches")).toBeVisible();
  await expect(page.getByRole("option", { name: new RegExp(`${t} In Contoso`) })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // The other tab followed: it says so, and its list shows Fabrikam's (empty) result.
  await expect(other.getByTestId("tab-notice")).toContainText("This tab switched to Fabrikam (synthetic)");
  await expect(other.getByTestId("shell-announcer")).toContainText("This tab switched to Fabrikam (synthetic)");
  await expect(other.getByRole("button", { name: /Organization\s*:\s*Fabrikam \(synthetic\)/ })).toBeVisible();
  await expect(contosoRow(other)).toHaveCount(0);
  await expect(other.getByText("No applications match these filters")).toBeVisible();
  await expectNoViolations(other, "A tab that followed a switch made in another tab");

  // A record made now belongs to Fabrikam; switch back through the palette and use the sidebar link.
  await post(page, "applications", { name: `${t} In Fabrikam`, lifecycle: "active" });
  await page.keyboard.press("Control+k");
  await paletteSearch.fill("switch");
  await page.getByRole("option", { name: "Switch organization to Contoso (synthetic)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeFocused();
  await expect(page.getByRole("button", { name: /Organization\s*:\s*Contoso \(synthetic\)/ })).toBeVisible();
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications" }).click();
  await page.getByLabel("Search applications").fill(t);
  await expect(contosoRow(page)).toBeVisible();
  await expect(table(page).getByRole("link", { name: `${t} In Fabrikam` })).toHaveCount(0);
  await context.close();
});

test("A user in one organization has no switcher; the organization's name is shown", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Switch organization/ })).toHaveCount(0);
  await expect(page.getByRole("menu", { name: "Switch organization" })).toHaveCount(0);
  await expect(page.locator("#primary-nav").locator("xpath=..")).toContainText("Contoso (synthetic)");
});
