import { expect, type Page, test } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** WCAG 1.4.10 reflow: no route makes the page itself scroll sideways on a phone (390 px)
 *  or at 400% zoom (320 px). Wide tables scroll inside their own region; chips, switches and headers wrap. Every
 *  route of the SPA an Admin can open is visited, with one record of each kind so the detail pages and the lists'
 *  tables have rows. */

test.describe.configure({ timeout: 600_000 });

/** Every route without a parameter, as an Admin opens it. Sign-in, setup, invitation, sign-up and the platform console
 *  are outside the signed-in shell and have their own specs. */
const STATIC = [
  "/", "/?scope=all", "/?scope=unassigned", "/dashboard", "/reports", "/data-quality", "/search?q=e2e", "/applications", "/applications?kind=components",
  "/applications/new", "/components/new", "/ai-agents", "/ai-agents/new", "/ai-models", "/ai-models/in-use", "/ai-providers", "/categories",
  "/tags", "/custom-fields", "/relationship-types", "/integrations", "/integrations/new", "/dependencies", "/vendor-risk",
  "/vendor-risk/documents", "/end-of-life", "/data-flows", "/assessments", "/assessments/new", "/renewals", "/renewals/new", "/contracts",
  "/contracts/new", "/license-pools", "/license-pools/new", "/discovery", "/discovery/connectors/new", "/discovery/setup",
  "/discovery/connections", "/discovery/runs", "/discovery/permissions", "/discovery/shadow-it", "/discovery/sso-coverage", "/profile",
  "/admin/people", "/admin/people?tab=vendors", "/admin/people?tab=teams", "/access-review", "/admin/users", "/admin/organization",
  "/admin/limits", "/admin/sign-in", "/admin/provisioning", "/admin/catalog-code", "/admin/ticket-connections", "/admin/audit",
  "/admin/system", "/admin/assistant", "/admin/assistant/usage", "/admin/digests", "/admin/notification-rules", "/admin/notification-rules/new",
  "/admin/notification-channels", "/admin/notification-log", "/admin/tokens", "/admin/webhooks", "/admin/webhooks/deliveries",
  "/admin/api/guide", "/admin/import-sources", "/imports",
];

const day = (offset: number) => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);

let records: Promise<string[]> | null = null;
/** One record of each kind with long names, and their detail and edit routes. */
function detailRoutes(page: Page) {
  records ??= (async () => {
    const t = token();
    const app = await post(page, "applications", { name: `${t} Enterprise resource planning suite`, lifecycle: "active", tags: [`${t}-finance-and-operations`] });
    const other = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
    const component = await post(page, "components", { name: `${t} Nightly export script`, lifecycle: "active", component_type: "script" });
    const agent = await post(page, "ai-agents", { name: `${t} Service desk agent`, lifecycle: "active" });
    const flow = await post(page, "integrations", { name: `${t} Payroll feed`, lifecycle: "active", source: app.id, target: other.id });
    const vendor = await post(page, "vendors", { name: `${t} Contoso Software Holdings` });
    const person = await post(page, "people", { name: `${t} Alexandra Featherstonehaugh`, email: `${t.toLowerCase()}@example.test` });
    const renewal = await post(page, "renewals", { name: `${t} ERP subscription`, auto_renew: "yes", renewal_date: day(120), applications: [app.id] });
    const contract = await post(page, "contracts", {
      name: `${t} ERP enterprise agreement`, amount: "125000.00", billing_frequency: "annual", start_date: day(-300), end_date: day(65),
      auto_renew: "yes", notice_value: 30, notice_unit: "days", vendor: vendor.id,
    });
    const pool = await post(page, "license-pools", { application: app.id, sku_name: `${t} E5`, seats_purchased: 100, seats_assigned: 80 });
    return [
      `/applications/${app.id}`, `/applications/${app.id}?tab=relationships`, `/applications/${app.id}/edit`, `/components/${component.id}`,
      `/ai-agents/${agent.id}`, `/integrations/${flow.id}`, `/integrations/${flow.id}/edit`, `/vendors/${vendor.id}`, `/people/${person.id}`,
      `/renewals/${renewal.id}`, `/renewals/${renewal.id}/edit`, `/contracts/${contract.id}`, `/contracts/${contract.id}/edit`,
      `/contracts/${contract.id}/renew`, `/license-pools/${pool.id}`,
    ];
  })();
  return records;
}

/** How far the page scrolls sideways, and the widest elements that cause it (not inside a scrolling region). */
async function overflow(page: Page) {
  return page.evaluate(() => {
    const de = document.documentElement;
    const over = de.scrollWidth - de.clientWidth;
    const wide: string[] = [];
    if (over > 0) {
      for (const el of document.querySelectorAll("body *")) {
        const r = el.getBoundingClientRect();
        if (r.right <= de.clientWidth + 1 || r.width === 0) continue;
        let a = el.parentElement;
        let contained = false;
        while (a && a !== document.body) {
          if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(a).overflowX)) { contained = true; break; }
          a = a.parentElement;
        }
        if (!contained) wide.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 50)} right=${Math.round(r.right)} "${(el.textContent ?? "").trim().slice(0, 30)}"`);
        if (wide.length >= 3) break;
      }
    }
    return { over, wide };
  });
}

async function check(page: Page, routes: string[], width: number) {
  await page.setViewportSize({ width, height: 800 });
  const failures: string[] = [];
  for (const route of routes) {
    await page.goto(route);
    await expect(page.locator("h1").first(), `${route} has a heading`).toBeVisible({ timeout: 15_000 });
    // Lists and panels fill in after the heading; measure once nothing is loading.
    await page.waitForLoadState("networkidle");
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    const { over, wide } = await overflow(page);
    if (over > 0) failures.push(`${route} scrolls ${over} px sideways at ${width} px: ${wide.join("; ")}`);
  }
  expect(failures, `pages wider than ${width} px`).toEqual([]);
}

for (const width of [390, 320]) {
  test(`No page scrolls sideways at ${width} px`, async ({ browser, baseURL }) => {
    const page = await pageAs(browser, baseURL, "admin@example.test");
    const details = await detailRoutes(page);
    await check(page, [...STATIC, ...details], width);
    await page.context().close();
  });
}

/** The record page's More menu at 320x700: it opens below when its own height fits there, even
 *  with less room below than its height cap (about 290 px free), and it stays inside the screen across (it used to
 *  start at x=-104). */
test("A More menu opens where it fits and stays on screen at 320 px", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const app = await post(page, "applications", { name: `${token()} Menu`, lifecycle: "active" });
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto(`/applications/${app.id}`);
  const more = page.getByRole("main").getByRole("button", { name: "More", exact: true });
  await expect(more).toBeVisible();
  // Put the button about 400 px down, leaving about 290 px below it.
  const top = (await more.boundingBox())!.y;
  await page.evaluate((dy) => window.scrollBy(0, dy), top - 400);
  await more.click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  const b = (await more.boundingBox())!;
  const m = (await menu.boundingBox())!;
  expect(page.viewportSize()!.height - (b.y + b.height), "room below").toBeGreaterThan(m.height + 12);
  expect(m.y, "opens below the button").toBeGreaterThanOrEqual(b.y + b.height);
  expect(m.x, "left edge on screen").toBeGreaterThanOrEqual(8);
  expect(m.x + m.width, "right edge on screen").toBeLessThanOrEqual(320 - 8 + 0.5);
  await page.keyboard.press("Escape");
  await page.context().close();
});
