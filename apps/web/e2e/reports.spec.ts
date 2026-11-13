import { readFileSync } from "node:fs";
import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** The Reports page. Spend by vendor, department, category and cost centre (tabs in the URL) with the
 *  allocation detail; the renewals calendar with its months in the URL, overdue first, counted per month; licence
 *  utilisation; a CSV export of each (the request and the file); and the personal calendar feed: create (the https
 *  address shown once, never a webcal link), copy, the feed answering with an ICS, rotate (the old address 404s, the new
 *  one answers), revoke. Runs in the contracts spec organization (`e2e/spec-org`, `contracts`). axe in both themes. */

test.describe.configure({ mode: "serial", timeout: 180_000 });

const EDITOR = "ct-editor@example.test";
const VIEWER = "ct-viewer@example.test";
let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "contracts" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}
const day = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);

/** Click an export, and return the request it made and the downloaded file's text. */
async function exported(page: Page, button: ReturnType<Page["getByRole"]>, path: RegExp) {
  const [request, download] = await Promise.all([
    page.waitForRequest((r) => path.test(r.url())),
    page.waitForEvent("download"),
    button.click(),
  ]);
  return { url: request.url(), name: download.suggestedFilename(), text: readFileSync((await download.path())!, "utf-8").replace(/^\uFEFF/, "") }; // the CSVs start with a BOM for Excel
}

const t = token();
let contractId = "";

test("Spend: each grouping in the URL, named gaps, the allocation detail and the CSV", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const vendor = await post(page, "vendors", { name: `${t} Vendor` });
  const a = await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active" });
  const b = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  // Ends in 20 days with 30 days' notice: its cancellation deadline was 10 days ago, so it is overdue in the calendar.
  const c = await post(page, "contracts", {
    name: `${t} Spend suite`, vendor: vendor.id, amount: "12000.00", billing_frequency: "annual", start_date: day(-300), end_date: day(20),
    auto_renew: "yes", notice_value: 30, notice_unit: "days", status: "active", cost_center: `CC${t}`,
  });
  contractId = c.id;
  await post(page, `contracts/${c.id}/lines`, { record: a.id, allocation_percent: "60" });
  await post(page, `contracts/${c.id}/lines`, { record: b.id, allocation_percent: null });

  await page.goto("/reports");
  await expect(page.getByRole("heading", { level: 1, name: "Reports" })).toBeVisible();
  await expect(page.getByRole("navigation").getByRole("link", { name: "Reports" })).toBeVisible(); // Money section
  await expect(page.getByRole("tab", { name: "Spend", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "By vendor" })).toHaveAttribute("aria-selected", "true");
  const usd = page.locator("section[data-currency='USD']");
  await expect(usd.getByRole("region", { name: "Spend in USD by vendor table" }).getByRole("row", { name: new RegExp(`${t} Vendor`) })).toContainText("12,000.00 USD");
  // Allocation detail: the fixed share and the shared rest, to the cent.
  const detail = usd.getByRole("region", { name: "Allocation detail in USD table" });
  await expect(detail.getByRole("row", { name: new RegExp(`${t} Spend suite.*${t} Ledger`) })).toContainText("7,200.00 USD");
  await expect(detail.getByRole("row", { name: new RegExp(`${t} Spend suite.*${t} Payroll`) })).toContainText("4,800.00 USD");
  await expect(detail.getByRole("link", { name: `${t} Ledger` })).toHaveAttribute("href", `/applications/${a.id}`);
  await expectNoViolations(page, "Reports, spend by vendor");

  // Department: these records have none, a named gap (never folded, never "Missing").
  await page.getByRole("tab", { name: "By department" }).click();
  await expect(page).toHaveURL(/tab=spend&by=department/);
  await expect(usd.getByRole("region", { name: "Spend in USD by department table" }).getByRole("row", { name: /No department/ }).locator("[data-gap]").first()).toContainText("No department");
  await page.getByRole("tab", { name: "By category" }).click();
  await expect(page).toHaveURL(/by=category/);
  await page.getByRole("tab", { name: "By cost center" }).click();
  await expect(page).toHaveURL(/by=cost_center/);
  await expect(usd.getByRole("region", { name: "Spend in USD by cost center table" }).getByRole("row", { name: new RegExp(`CC${t}`) })).toContainText("12,000.00 USD");

  // The CSV follows the grouping.
  const csv = await exported(page, page.getByRole("button", { name: "Export CSV of spend by cost center" }), /reports\/spend\/export\.csv\?by=cost_center/);
  expect(csv.name).toMatch(/^softwarden-spend-by-cost-center-\d{4}-\d{2}-\d{2}\.csv$/);
  expect(csv.text.split(/\r?\n/)[0]).toBe("group,gap,currency,amount,percent_of_contract,contract,contract_id,vendor,record,record_kind,record_id,cost_center");
  expect(csv.text).toContain(`${t} Spend suite`);
  await expect(page.getByRole("status").filter({ hasText: /^Downloaded softwarden-spend-by-cost-center/ })).toBeVisible();

  // A reload keeps the tab and the grouping.
  await page.reload();
  await expect(page.getByRole("tab", { name: "By cost center" })).toHaveAttribute("aria-selected", "true");
  await page.context().close();
});

test("Renewals calendar: months in the URL, overdue first, per month, and the CSV; license utilization and its CSV", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  await page.goto("/reports");
  await page.getByRole("tab", { name: "Renewals calendar" }).click();
  await expect(page).toHaveURL(/tab=calendar/);
  const overdue = page.getByTestId("calendar-overdue");
  const row = overdue.getByRole("row", { name: new RegExp(`${t} Spend suite`) });
  await expect(row).toContainText(/Overdue by 10 days/);
  await expect(row.getByRole("link", { name: `${t} Spend suite` })).toHaveAttribute("href", `/contracts/${contractId}`);
  await expect(page.getByRole("region", { name: "Deadlines by month table" }).getByRole("row")).toHaveCount(13); // heading + 12 months
  await page.getByLabel("Months ahead").selectOption("3");
  await expect(page).toHaveURL(/months=3/);
  await expect(page.getByRole("region", { name: "Deadlines by month table" }).getByRole("row")).toHaveCount(4);
  await expectNoViolations(page, "Reports, renewals calendar");
  const csv = await exported(page, page.getByRole("button", { name: "Export CSV of the renewals calendar" }), /reports\/renewals-calendar\/export\.csv\?months=3/);
  expect(csv.name).toMatch(/^softwarden-renewals-calendar-\d{4}-\d{2}-\d{2}\.csv$/);
  expect(csv.text.split(/\r?\n/)[0]).toBe("date,days,overdue,action,label,name,source,vendor,term_ends,auto_renew,decision,contract,annualized_cost,currency,owner,id");
  expect(csv.text).toContain(`${t} Spend suite`);

  await page.getByRole("tab", { name: "License utilization" }).click();
  await expect(page).toHaveURL(/tab=licences/);
  const lic = await exported(page, page.getByRole("button", { name: "Export CSV of license utilization" }), /reports\/license-utilization\/export\.csv$/);
  expect(lic.name).toMatch(/^softwarden-licence-utilisation-\d{4}-\d{2}-\d{2}\.csv$/);
  expect(lic.text.split(/\r?\n/)[0]).toBe("application,application_id,archived,sku_name,sku_id,pools,seats_purchased,seats_assigned,seats_available,utilization,overused");
  await page.context().close();
});

test("Calendar feed: create (shown once, https, no webcal), copy, subscribe, rotate (the old address stops), revoke", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseURL });
  // One feed per person: start from none.
  await post(page, "calendar-feed", undefined, "DELETE");
  await page.goto("/reports?tab=calendar");
  const panel = page.getByTestId("calendar-feed");
  await expect(panel).toContainText("You have no calendar feed.");
  await expect(panel.getByRole("heading", { name: "How to subscribe" })).toBeVisible();
  await expect(panel).toContainText("Add calendar, then Subscribe from web");
  await expect(panel).toContainText("From URL");
  await expect(panel).toContainText("New Calendar Subscription");

  await panel.getByRole("button", { name: "Create calendar feed" }).click();
  const box = panel.getByLabel("Calendar feed address");
  await expect(box).toBeVisible();
  await expect(box).toBeFocused(); // the button that was pressed is gone: focus moves to the new address
  const first = await box.inputValue();
  // The feed's https address behind Caddy (http on this test server), with the secret in its query string.
  expect(first).toMatch(/^https?:\/\/[^/]+\/api\/v1\/calendar\/renewals\.ics\?token=swc_[A-Za-z0-9_-]{40}$/);
  await expect(panel).not.toContainText("webcal");
  await expect(panel.getByTestId("feed-address")).toContainText("Copy this address now: it is shown only once.");
  await panel.getByRole("button", { name: "Copy address" }).click();
  await expect(panel.getByRole("status").filter({ hasText: "Copied the address." })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(first);
  await expect(panel.getByTestId("feed-last-used")).toHaveText("Not yet: no calendar has fetched it");
  await expectNoViolations(page, "Calendar feed, address shown once");

  // A calendar app fetches it: an ICS calendar.
  const local = (url: string) => { const u = new URL(url); return `${u.pathname}${u.search}`; };
  const ics = await page.request.get(local(first), { headers: { Accept: "text/calendar" } });
  expect(ics.status()).toBe(200);
  expect(ics.headers()["content-type"]).toContain("text/calendar");
  const body = await ics.text();
  expect(body).toContain("BEGIN:VCALENDAR");
  expect(body).toContain(`${t} Spend suite`);

  // After a reload the address isn't shown again: its prefix, when made and when last fetched.
  await page.reload();
  await expect(panel.getByLabel("Calendar feed address")).toHaveCount(0);
  await expect(panel.getByTestId("feed-facts")).toContainText(`${first.split("token=")[1].slice(0, 8)}`);
  await expect(panel.getByTestId("feed-last-used")).not.toHaveText(/Not yet/);

  // Rotate: the dialog warns that subscriptions break; the old address stops at once.
  await panel.getByRole("button", { name: "Rotate address…" }).click();
  const rotate = page.getByRole("dialog", { name: "Rotate your calendar feed address?" });
  await expect(rotate).toContainText("every calendar subscribed to it stops updating");
  await expectNoViolations(page, "Rotate calendar feed dialog");
  await rotate.getByRole("button", { name: "Cancel" }).click();
  await expect(panel.getByRole("button", { name: "Rotate address…" })).toBeFocused();
  await panel.getByRole("button", { name: "Rotate address…" }).click();
  await rotate.getByRole("button", { name: "Rotate address" }).click();
  await expect(rotate).toHaveCount(0);
  await expect(panel.getByLabel("Calendar feed address")).toBeFocused();
  const second = await panel.getByLabel("Calendar feed address").inputValue();
  expect(second).not.toBe(first);
  expect(second).toMatch(/\/api\/v1\/calendar\/renewals\.ics\?token=swc_/);
  await expect(panel.getByTestId("feed-address")).toContainText("The previous address no longer works.");
  expect((await page.request.get(local(first))).status()).toBe(404);
  expect((await page.request.get(local(second))).status()).toBe(200);

  // Revoke: the address stops, and the panel offers a new feed.
  await panel.getByRole("button", { name: "Revoke…" }).click();
  const revoke = page.getByRole("dialog", { name: "Revoke your calendar feed?" });
  await expectNoViolations(page, "Revoke calendar feed dialog");
  await revoke.getByRole("button", { name: "Revoke feed" }).click();
  await expect(panel).toContainText("You have no calendar feed.");
  await expect(panel.getByRole("button", { name: "Create calendar feed" })).toBeFocused(); // not the page body
  await expect(panel.getByRole("status").filter({ hasText: "Calendar feed revoked." })).toHaveCount(1);
  expect((await page.request.get(local(second))).status()).toBe(404);
  await page.context().close();
});
