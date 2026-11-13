import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PASSWORD, pageAs, post, token } from "./helpers";

/** The dashboard. Every block from `GET /dashboard` (a fixed body through a route, so the numbers,
 *  currencies, gaps and labels are known): Needs attention counts linking to that page per scope, deadlines with the
 *  overdue / 30 / 60 / 90 toggles in the URL and links to the renewal or contract, a deadline whose label is null shown by
 *  its words alone, expiring API tokens, spend per currency (never summed) by vendor, department and category as a chart
 *  with the same rows in a table (named gaps as gap chips, "Everything else" apart), licence utilisation and data
 *  quality. Null blocks (a capability the edition lacks) read as unavailable, never zero. Then the real dashboard per
 *  role in the contracts spec organization: everyone's home (first under Work), an overdue contract deadline linking to
 *  the contract, the Admin's expiring token that a Viewer doesn't see. axe in both themes. */

test.describe.configure({ timeout: 180_000 });

const U = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const user = { id: 1, name: "Ines Okafor", email: "ines@example.test" };

const DASHBOARD = {
  as_of: "2026-11-05",
  needs_attention: {
    counts: { mine: 2, all: 7, unassigned: 1 },
    queues: [
      { queue: "reviews", all: 4, mine: 2, unassigned: 0, counted: true },
      { queue: "seat_counts", all: 3, mine: 0, unassigned: 1, counted: false },
    ],
  },
  deadlines: {
    overdue: 1, total: 3, windows: [{ days: 30, count: 1 }, { days: 60, count: 2 }, { days: 90, count: 2 }],
    rows: [
      {
        uid: `renewal-cycle-${U(11)}@softwarden`, source: "renewal", id: U(1), name: "Fixture Mail renewal", vendor: "Contoso Mail", date: "2026-10-01",
        action: "cancel_by", label: "calculated", label_text: "Calculated", explanation: "Renewal date minus the 30 day notice period.",
        term_ends: "2026-10-31", auto_renew: "yes", decision: null, timing: { days: -35, overdue: true, text: "Overdue by 35 days" },
        contract: { id: U(2), name: "Fixture Mail EA" }, annualized_cost: "12000.00", currency: "USD", owner: user, path: `/renewals/${U(1)}`,
      },
      {
        uid: `contract-${U(3)}@softwarden`, source: "contract", id: U(3), name: "Fixture Chat", vendor: null, date: "2026-11-17",
        action: "cancel_by", label: null, label_text: "Cancellation deadline", explanation: "",
        term_ends: "2026-12-17", auto_renew: "unknown", decision: null, timing: { days: 12, overdue: false, text: "in 12 days" },
        contract: { id: U(3), name: "Fixture Chat" }, annualized_cost: "4800.00", currency: "EUR", owner: null, path: `/contracts/${U(3)}`,
      },
      {
        uid: `contract-${U(4)}@softwarden`, source: "contract", id: U(4), name: "Fixture Wiki", vendor: "Atlassian", date: "2026-12-25",
        action: "renew_by", label: "assumed", label_text: "Assumed", explanation: "End date minus the organization's 60 day lead time.",
        term_ends: "2027-02-23", auto_renew: "no", decision: null, timing: { days: 50, overdue: false, text: "in 50 days" },
        contract: { id: U(4), name: "Fixture Wiki" }, annualized_cost: null, currency: "USD", owner: null, path: `/contracts/${U(4)}`,
      },
    ],
  },
  expiring_credentials: {
    days: 30, count: 2,
    rows: [
      { kind: "api_token", id: U(21), name: "Fixture CI token", prefix: "swt_ab12", owner: user, expires_on: "2026-11-02", days: -3, expired: true },
      { kind: "api_token", id: U(22), name: "Fixture sync token", prefix: "swt_cd34", owner: user, expires_on: "2026-11-10", days: 5, expired: false },
    ],
  },
  spend: {
    totals: [
      { currency: "EUR", annualized: "184800.00", run_rate: "184800.00", one_time: "0.00", future: "0.00", future_one_time: "0.00", next_start: null, contracts: 2 },
      { currency: "USD", annualized: "12800.00", run_rate: "12000.00", one_time: "800.00", future: "6000.00", future_one_time: "0.00", next_start: "2027-01-01", contracts: 3 },
    ],
    by_vendor: [
      { key: U(31), label: "Veeva", gap: null, currency: "EUR", amount: "180000.00", contracts: 1, records: 1 },
      { key: null, label: "No vendor", gap: "no_vendor", currency: "EUR", amount: "4800.00", contracts: 1, records: 0 },
      { key: U(32), label: "Contoso Mail", gap: null, currency: "USD", amount: "12000.00", contracts: 1, records: 2 },
      { key: null, label: "No vendor", gap: "no_vendor", currency: "USD", amount: "500.00", contracts: 1, records: 1 },
      { key: null, label: "Everything else", gap: "other", currency: "USD", amount: "300.00", contracts: null, records: null, groups: 3 },
    ],
    by_department: [
      { key: U(33), label: "IT", gap: null, currency: "USD", amount: "8000.00", contracts: 2, records: 2 },
      { key: null, label: "Not on a record", gap: "unallocated", currency: "USD", amount: "4800.00", contracts: 1, records: null },
      { key: U(34), label: "Quality", gap: null, currency: "EUR", amount: "184800.00", contracts: 2, records: 1 },
    ],
    by_category: [
      { key: U(35), label: "Productivity", gap: null, currency: "USD", amount: "12800.00", contracts: 3, records: 3 },
      { key: U(36), label: "Quality management", gap: null, currency: "EUR", amount: "184800.00", contracts: 2, records: 1 },
    ],
  },
  license_utilization: {
    total: { seats_purchased: 150, seats_assigned: 160, seats_available: -10, utilization: 106.7, overused: true, pools: 2, overused_rows: 1 },
    rows: [
      { application: { id: U(41), name: "Fixture Teams", archived: false }, sku_name: "Microsoft 365 E5", sku_id: "sku-e5", pools: 1, seats_purchased: 50, seats_assigned: 60, seats_available: -10, utilization: 120, overused: true },
      { application: { id: U(42), name: "Fixture Wiki app", archived: false }, sku_name: "Confluence Premium", sku_id: "", pools: 1, seats_purchased: 100, seats_assigned: 100, seats_available: 0, utilization: 100, overused: false },
    ],
  },
  data_quality: {
    as_of: "2026-11-05", score: 80, records: 5, complete: 3, passing: 40, failing: 10, excepted: 0,
    by_kind: [{ kind: "application", records: 5, complete: 3, passing: 40, failing: 10, excepted: 0, score: 80 }],
    by_rule: [{ id: "business_owner", label: "Business owner", severity: "high", records: 5, passing: 4, failing: 1, excepted: 0, score: 80 }],
    rules: { on: 5, off: 0, unavailable: 0 },
  },
};

async function withDashboard(page: Page, body: unknown) {
  await page.route("**/api/v1/dashboard", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) }));
}

test("Every block from a known dashboard: counts into queues, deadlines in windows, credentials, spend per currency as chart and table, licenses, data quality", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  // At least one record, so Needs attention shows its queues (an empty catalog shows its empty state instead).
  await post(page, "applications", { name: `${token()} Dashboard`, lifecycle: "active" });
  await withDashboard(page, DASHBOARD);
  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Where Contoso", { exact: false }).first()).toContainText("2026-11-05");

  // Needs attention: the three scopes, each opening Needs attention in that scope. No per-queue table:
  // Needs attention itself lists every queue.
  const attention = page.getByTestId("dash-attention");
  await expect(attention.getByTestId("dash-attention-mine-value")).toContainText("2");
  await expect(attention.getByTestId("dash-attention-all-value")).toContainText("7");
  await expect(attention.getByRole("link", { name: /^7 items in All, open Needs attention/ })).toHaveAttribute("href", "/?scope=all");
  await expect(attention.getByRole("link", { name: /^2 items in Assigned to me, open Needs attention/ })).toHaveAttribute("href", "/");
  await expect(attention.getByRole("link", { name: /^1 item in Unassigned, open Needs attention/ })).toHaveAttribute("href", "/?scope=unassigned");
  await expect(attention.getByRole("table")).toHaveCount(0);

  // Deadlines: the toggles count each window and filter the rows; the choice is in the URL.
  const deadlines = page.getByTestId("dash-deadlines");
  const table = deadlines.getByRole("region", { name: "Deadlines table" });
  await expect(table.getByRole("row")).toHaveCount(4); // the heading row and three deadlines
  await expect(table.getByRole("link", { name: "Fixture Mail renewal" })).toHaveAttribute("href", `/renewals/${U(1)}`);
  await expect(table.getByRole("link", { name: "Fixture Mail EA" })).toHaveAttribute("href", `/contracts/${U(2)}`);
  await expect(table.getByRole("link", { name: "Fixture Chat" })).toHaveAttribute("href", `/contracts/${U(3)}`);
  // A stored deadline its computation doesn't explain: no label chip, its words alone, no explanation.
  const chat = table.getByRole("row", { name: /Fixture Chat/ });
  await expect(chat.locator("[data-label='']")).toHaveText("Cancellation deadline");
  await expect(chat.locator(".sw-chip[data-label]")).toHaveCount(0);
  await expect(chat).toContainText("No vendor"); // a gap chip, never "Missing"
  await expect(table.getByRole("row", { name: /Fixture Wiki/ }).locator("[data-label='assumed']")).toContainText("Assumed");
  await expect(table.getByRole("row", { name: /Fixture Mail renewal/ })).toContainText("Overdue by 35 days");
  await deadlines.getByRole("button", { name: /^Within 30 days/ }).click();
  await expect(page).toHaveURL(/[?&]due=30/);
  await expect(deadlines.getByRole("button", { name: /^Within 30 days/ })).toHaveAttribute("aria-pressed", "true");
  await expect(table.getByRole("row")).toHaveCount(2);
  await expect(table).toContainText("Fixture Chat");
  await deadlines.getByRole("button", { name: /^Overdue/ }).click();
  await expect(page).toHaveURL(/[?&]due=overdue/);
  await expect(table).toContainText("Fixture Mail renewal");
  await expect(table).not.toContainText("Fixture Chat");
  await expect(deadlines.getByRole("link", { name: "Renewals calendar" })).toHaveAttribute("href", "/reports?tab=calendar");

  // Credentials: expired and expiring, in words.
  const creds = page.getByTestId("dash-credentials");
  await expect(creds.getByRole("row", { name: /Fixture CI token/ })).toContainText("Expired 3 days ago");
  await expect(creds.getByRole("row", { name: /Fixture sync token/ })).toContainText("Expires in 5 days");

  // Spend: one currency at a time (the first, EUR), its totals, the chart and the same rows in the table.
  const spend = page.getByTestId("dash-spend");
  await expect(spend.getByRole("button", { name: "EUR" })).toHaveAttribute("aria-pressed", "true");
  await expect(spend.getByTestId("dash-spend-eur-annualized-value")).toHaveText("184,800.00 EUR");
  await expect(spend.getByTestId("spend-chart")).toContainText("Veeva");
  await expect(spend.getByTestId("spend-chart")).toContainText("180,000.00");
  await spend.getByRole("button", { name: "USD" }).click();
  await expect(page).toHaveURL(/[?&]currency=USD/);
  await expect(spend.getByTestId("dash-spend-usd-annualized-value")).toHaveText("12,800.00 USD");
  await expect(spend.getByTestId("dash-spend-usd-future-value")).toHaveText("6,000.00 USD");
  const vendorTable = spend.getByRole("region", { name: "Spend by vendor table" });
  await expect(vendorTable.getByRole("row", { name: /Contoso Mail/ })).toContainText("12,000.00 USD");
  await expect(vendorTable.getByRole("row", { name: /No vendor/ }).locator("[data-gap]").first()).toContainText("⚠No vendor");
  await expect(vendorTable.getByRole("row", { name: /Everything else/ })).toContainText("Everything else (3 more)");
  await expect(vendorTable).not.toContainText("Veeva"); // EUR is never mixed in
  await expect(spend.getByTestId("spend-chart")).toContainText("⚠ No vendor");
  await expect(spend.getByTestId("spend-chart")).toContainText("… Everything else");
  await expect(spend.getByRole("list", { name: "Legend" })).toContainText("A named gap");
  await spend.getByRole("tab", { name: "By department" }).click();
  await expect(page).toHaveURL(/[?&]spend=department/);
  const deptTable = spend.getByRole("region", { name: "Spend by department table" });
  await expect(deptTable.getByRole("row", { name: /Not on a record/ })).toContainText("4,800.00 USD");
  await spend.getByRole("tab", { name: "By category" }).click();
  await expect(page).toHaveURL(/[?&]spend=category/);
  await expect(spend.getByRole("region", { name: "Spend by category table" })).toContainText("Productivity");

  // Licences: overuse said in words.
  const licences = page.getByTestId("dash-licences");
  await expect(licences.getByRole("row", { name: /Fixture Teams/ })).toContainText("60 of 50 seats (120%)");
  await expect(licences.getByRole("row", { name: /Fixture Teams/ })).toContainText("Overused by 10 seats");
  await expect(licences.getByTestId("dash-seats-overused-value")).toHaveText("1");

  // Data quality: the panel, from the same response.
  const dq = page.locator("section[aria-labelledby='h-dq']");
  await expect(dq.getByTestId("stat-compliance-value")).toHaveText("80%");

  // The URL holds every choice: a reload shows the same.
  await page.reload();
  await expect(page.getByTestId("dash-spend").getByRole("tab", { name: "By category" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("dash-spend").getByRole("button", { name: "USD" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("dash-deadlines").getByRole("button", { name: /^Overdue/ })).toHaveAttribute("aria-pressed", "true");
  await expectNoViolations(page, "Dashboard with every block");

  // The All tile lands on Needs attention in that scope, whose jump links reach every queue.
  await page.getByTestId("dash-attention").getByRole("link", { name: /^7 items in All/ }).click();
  await expect(page).toHaveURL(/\/\?scope=all$/);
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  await page.context().close();
});

test("Blocks the edition lacks are unavailable, not zero; an empty organization says what each block needs", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "viewer@example.test");
  await withDashboard(page, { ...DASHBOARD, deadlines: null, spend: null, license_utilization: null, data_quality: null, expiring_credentials: { days: 30, count: 0, rows: [] } });
  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(page.getByTestId("dash-deadlines")).toContainText("Deadlines isn't available.");
  await expect(page.getByTestId("dash-spend")).toContainText("Spend isn't available. This organization's edition doesn't include contracts.");
  await expect(page.getByTestId("dash-licences")).toContainText("License utilization isn't available.");
  await expect(page.locator("section[aria-labelledby='h-dq']")).toContainText("Data quality isn't available.");
  for (const id of ["dash-deadlines", "dash-spend", "dash-licences"]) await expect(page.getByTestId(id).locator("table")).toHaveCount(0);
  await expect(page.getByTestId("dash-credentials")).toContainText("Your API tokens");
  await expect(page.getByTestId("dash-credentials")).toContainText("No API tokens expire in the next 30 days.");
  await expectNoViolations(page, "Dashboard with unavailable blocks");

  // No live contracts, no pools: what each block holds and how to fill it.
  await page.unroute("**/api/v1/dashboard");
  await withDashboard(page, {
    ...DASHBOARD, deadlines: { overdue: 0, total: 0, windows: [{ days: 30, count: 0 }, { days: 60, count: 0 }, { days: 90, count: 0 }], rows: [] },
    spend: { totals: [], by_vendor: [], by_department: [], by_category: [] },
    license_utilization: { total: { seats_purchased: 0, seats_assigned: 0, seats_available: 0, utilization: null, overused: false, pools: 0, overused_rows: 0 }, rows: [] },
  });
  await page.reload();
  await expect(page.getByTestId("dash-deadlines")).toContainText("No action deadlines in the next 90 days, and none overdue.");
  await expect(page.getByTestId("dash-spend")).toContainText("No spend yet");
  await expect(page.getByTestId("dash-licences")).toContainText("No license pools yet");

  // A failed load says so, with Retry.
  await page.unroute("**/api/v1/dashboard");
  await page.route("**/api/v1/dashboard", (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Something went wrong." }) }));
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("Something went wrong. Nothing was changed.");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await page.context().close();
});

// ---- the real dashboard, per role ----------------------------------------------------------------------------------------

const ADMIN = "ct-admin@example.test";
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

test("The real dashboard: first under Work for every role, an overdue contract deadline linking to it, the Admin's expiring token hidden from a Viewer", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const t = token();
  // A cancellation deadline seven months before an end date tomorrow: long overdue, so among the 10 soonest.
  const late = await post(admin, "contracts", {
    name: `${t} Overdue agreement`, amount: "2400.00", billing_frequency: "annual", start_date: day(-300), end_date: day(1), auto_renew: "yes",
    notice_value: 7, notice_unit: "months", status: "active",
  });
  const me = await (await admin.request.get("/api/v1/auth/me")).json();
  await post(admin, "api-tokens", { user: me.user.id, name: `${t} expiring`, read_only: true, expires_on: day(10) });

  await admin.goto("/dashboard");
  await expect(admin.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  // Everyone's home: first under Work, Needs attention second.
  const work = admin.getByRole("navigation").getByRole("group", { name: "Work" });
  await expect(work.getByRole("link").first()).toHaveText(/Dashboard/);
  await expect(work.getByRole("link").nth(1)).toHaveText(/^Needs attention/);
  const deadlines = admin.getByTestId("dash-deadlines");
  await deadlines.getByRole("button", { name: /^Overdue/ }).click();
  const row = deadlines.getByRole("row", { name: new RegExp(`${t} Overdue agreement`) });
  await expect(row).toContainText(/Overdue by \d+ days/);
  await expect(row).toContainText("2,400.00 USD");
  await expect(admin.getByTestId("dash-credentials").getByRole("row", { name: new RegExp(`${t} expiring`) })).toContainText("Expires in 10 days");
  await expect(admin.getByTestId("dash-spend").getByRole("heading", { name: "Spend", exact: true })).toBeVisible();
  await expect(admin.getByTestId("dash-licences")).toBeVisible();
  await expectNoViolations(admin, "Dashboard with real data (Admin)");
  await row.getByRole("link", { name: `${t} Overdue agreement` }).click();
  await expect(admin).toHaveURL(new RegExp(`/contracts/${late.id}$`));
  await expect(admin.getByRole("heading", { level: 1, name: `${t} Overdue agreement` })).toBeVisible();
  await admin.context().close();

  // A Viewer reads the same dashboard, also first under Work; their credentials block lists only their own tokens.
  const viewer = await as(browser, baseURL, VIEWER);
  await viewer.goto("/dashboard");
  await expect(viewer.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  const vwork = viewer.getByRole("navigation").getByRole("group", { name: "Work" });
  await expect(vwork.getByRole("link").first()).toHaveText(/Dashboard/);
  await expect(vwork.getByRole("link").nth(1)).toHaveText(/^Needs attention/);
  await expect(viewer.getByTestId("dash-credentials")).toContainText("Your API tokens");
  await expect(viewer.getByTestId("dash-credentials")).not.toContainText(`${t} expiring`);
  await expect(viewer.getByTestId("dash-deadlines").getByRole("link", { name: `${t} Overdue agreement` })).toBeVisible();
  await viewer.context().close();
});

test("After sign-in every role lands on the Dashboard; `/` stays Needs attention; a page asked for wins", async ({ browser, baseURL }) => {
  const signIn = async (email: string, path = "/login") => {
    const page = await (await browser.newContext({ baseURL })).newPage();
    await page.goto(path);
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    return page;
  };
  // Owner decision 2026-11-05: the Dashboard is everyone's home, not only an Admin's.
  for (const email of ["admin@example.test", "editor@example.test", "viewer@example.test"]) {
    const page = await signIn(email);
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
    // `/` is still Needs attention, and /login while signed in goes home.
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
    await page.goto("/login");
    await expect(page).toHaveURL(/\/dashboard$/);
    await page.context().close();
  }
  // A shared link: the Viewer signs in and goes where it pointed, filters included.
  const linked = await signIn("viewer@example.test", "/reports?tab=calendar");
  await expect(linked).toHaveURL(/\/reports\?tab=calendar$/);
  await linked.context().close();
  // A `next` with a control character (a browser would strip the tab into "//evil.example") counts as no page: home.
  const sneaky = await signIn("viewer@example.test", "/login?next=/%09/evil.example");
  await expect(sneaky).toHaveURL(/\/dashboard$/);
  await expect(sneaky.getByRole("link", { name: "Sign in with Microsoft Entra ID" })).toHaveCount(0);
  await sneaky.context().close();
});
