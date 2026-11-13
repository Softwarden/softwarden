import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** The dashboard's deadlines stack on a phone, sidebar sections close, the all-clear queues share one line, no Kind
 *  switch on Applications, one outline button color, a timezone picker in Organization settings, and tabs wrap on a
 *  phone. */

test.describe.configure({ timeout: 120_000 });

const U = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

test("On a phone the dashboard's deadlines are stacked rows with their labels, inside the page", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/v1/dashboard", async (route) => {
    const body = await (await route.fetch()).json();
    const row = {
      uid: `renewal-${U(1)}@softwarden`, source: "renewal", id: U(1), name: "Payroll connector license", vendor: "Contoso", date: "2026-11-20",
      action: "cancel_by", label: "calculated", label_text: "Calculated", explanation: "Renewal date 2026-12-20 minus notice period 30 days.",
      term_ends: "2026-12-20", auto_renew: "yes", decision: null, timing: { days: 10, overdue: false, text: "in 10 days" },
      contract: null, annualized_cost: "1200.00", currency: "USD", owner: null, path: `/renewals/${U(1)}`,
    };
    body.deadlines = { overdue: 0, total: 1, windows: [{ days: 30, count: 1 }, { days: 60, count: 1 }, { days: 90, count: 1 }], rows: [row] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/dashboard");
  const table = page.getByTestId("dash-deadlines").getByRole("table");
  await expect(table).toBeVisible();
  // Every cell of the row is inside the screen: nothing is cut off at the side or left to a sideways scroll.
  const cells = table.locator("tbody tr").first().locator("td, th");
  for (const cell of await cells.all()) {
    const box = (await cell.boundingBox())!;
    expect(box.x + box.width).toBeLessThanOrEqual(390);
  }
  await expect(table.locator("td[data-label='Action']")).toContainText("Cancel by");
  expect(await table.evaluate((t) => t.scrollWidth <= t.clientWidth + 1)).toBe(true);
  await page.context().close();
});

test("A sidebar section closes and stays closed; the current page's section always shows", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.goto("/dashboard");
  const nav = page.getByRole("navigation", { name: "Primary" });
  const admin = nav.getByRole("button", { name: "Admin" });
  await expect(admin).toHaveAttribute("aria-expanded", "true");
  await admin.click();
  await expect(admin).toHaveAttribute("aria-expanded", "false");
  await expect(nav.getByRole("link", { name: "Audit log" })).toBeHidden();
  await page.reload();
  await expect(nav.getByRole("button", { name: /^Admin/ })).toHaveAttribute("aria-expanded", "false");
  // Opening an Admin page shows its section, closed or not.
  await page.goto("/admin/audit");
  await expect(nav.getByRole("link", { name: "Audit log" })).toBeVisible();
  await page.goto("/dashboard");
  await nav.getByRole("button", { name: /^Admin/ }).click();
  await expect(nav.getByRole("link", { name: "Audit log" })).toBeVisible();
  await expectNoViolations(page, "Sidebar with section disclosures");
  await page.context().close();
});

test("Needs attention lists the queues with items first and the all-clear ones on one line", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.route("**/api/v1/needs-attention*", async (route) => {
    const body = await (await route.fetch()).json();
    for (const k of Object.keys(body)) if (Array.isArray(body[k]) && k !== "ownership_gaps") body[k] = [];
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await post(page, "applications", { name: `${token()} Ownerless`, lifecycle: "active" });
  await page.goto("/?scope=all");
  const clear = page.getByTestId("all-clear");
  await expect(clear).toContainText("Nothing in AI agent gaps, Reviews, Links to verify");
  // One line: the all-clear sentences are behind the disclosure, and the first queue on the page has work in it.
  const toggle = clear.getByRole("button", { name: /^Nothing waiting \(\d+\)$/ });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("No reviews due. Every active record has a current review.")).toBeHidden();
  const first = page.getByRole("main").locator("section").first();
  await expect(first).toHaveAttribute("aria-labelledby", "h-owners");
  // One card whose sentence names the queues (it may wrap), not a card per queue (14 x 53 px before).
  expect((await clear.boundingBox())!.height).toBeLessThan(140);
  // Acceptance (1440x900): the first actionable row starts on the first screen.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.evaluate(() => window.scrollTo(0, 0));
  const firstRow = page.getByRole("region", { name: "Queue table: Ownership gaps" }).locator("tbody tr").first();
  expect((await firstRow.boundingBox())!.y).toBeLessThan(900);
  await toggle.click();
  await expect(page.getByText("No reviews due. Every active record has a current review.")).toBeVisible();
  // The jump links list only the queues with items; a link to an all-clear queue (the dashboard's) opens the line and
  // lands on its heading.
  const jumps = page.getByRole("navigation", { name: "Queues" });
  await expect(jumps.getByRole("link", { name: /^Ownership gaps/ })).toBeVisible();
  await expect(jumps.getByRole("link", { name: /^Reviews/ })).toHaveCount(0);
  await page.goto("/?scope=all#h-reviews");
  await expect(page.getByRole("heading", { name: /^Reviews/ })).toBeFocused();
  await expectNoViolations(page, "Needs attention with the all-clear line open");
  await page.context().close();
});

test("Outline buttons that are links have the same text color as outline buttons", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.goto("/applications");
  const head = page.getByRole("main");
  const button = head.getByRole("button", { name: "Export CSV…" });
  const link = head.getByRole("link", { name: "Import CSV" });
  const color = (l: typeof button) => l.evaluate((el) => getComputedStyle(el).color);
  expect(await color(link)).toBe(await color(button));
  await page.context().close();
});

test("Organization settings chooses the timezone from the list of zones", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.goto("/admin/organization");
  const zone = page.getByRole("combobox", { name: "Timezone" });
  await expect(zone).toHaveValue(/.+/);
  await zone.fill("Tokyo");
  await expect(page.getByRole("listbox", { name: "Timezones" }).getByRole("option", { name: "Asia/Tokyo" })).toBeVisible();
  await expectNoViolations(page, "Organization settings with the timezone list open");
  await page.keyboard.press("Escape");
  await page.context().close();
});

test("On a phone a page's tabs wrap instead of running off the screen", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.setViewportSize({ width: 320, height: 800 });
  const app = await post(page, "applications", { name: `${token()} Tabs`, lifecycle: "active" });
  await page.goto(`/applications/${app.id}`);
  const tabs = page.getByRole("tab");
  await expect(tabs.first()).toBeVisible();
  expect(await tabs.count()).toBeGreaterThan(3);
  for (const tab of await tabs.all()) {
    const box = (await tab.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(320);
  }
  await page.context().close();
});
