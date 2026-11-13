import { expect, type Page, test } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** Headings, document titles and the app's own words for refusals, uploads, currencies and sessions. */

test.describe.configure({ timeout: 120_000 });

/** The visible headings' levels, in order, as assistive technology reads them. */
async function headingLevels(page: Page) {
  return page.evaluate(() => [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading]")]
    .filter((h) => (h as HTMLElement).offsetParent !== null || getComputedStyle(h).position === "fixed")
    .map((h) => Number(h.getAttribute("aria-level") ?? h.tagName.slice(1))));
}

/** Where a heading goes deeper by more than one level ("1>3"). */
const skips = (levels: number[]) => levels.flatMap((l, i) => (i > 0 && l > levels[i - 1] + 1 ? [`${levels[i - 1]}>${l}`] : []));

test("One h1 on the API reference; License pools and Data flows don't skip from h1 to h3", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const t = token();
  const app = await post(page, "applications", { name: `${t} Office`, lifecycle: "active" });
  await post(page, "license-pools", { application: app.id, sku_name: `${t} E3`, seats_purchased: 10, seats_assigned: 4 });
  const other = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  await post(page, "integrations", { name: `${t} Feed`, lifecycle: "active", source: app.id, target: other.id, data_classification: "confidential" });

  await page.goto("/admin/api");
  await expect(page.getByTestId("api-reference").getByRole("heading").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("h1:visible")).toHaveCount(1);
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);

  for (const route of ["/license-pools", "/data-flows"]) {
    await page.goto(route);
    await expect(page.locator("main section[aria-labelledby]").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    const levels = await headingLevels(page);
    expect(levels[0], `${route} starts with its h1`).toBe(1);
    expect(skips(levels), `${route} heading levels ${levels.join(",")}`).toEqual([]);
  }
  await page.context().close();
});

test("A throttled or refused query says so in words, not the framework's sentence", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const refuse = (status: number, detail: string, type: string, headers: Record<string, string> = {}) => (route: import("@playwright/test").Route) =>
    route.request().method() === "GET"
      ? route.fulfill({ status, headers, contentType: "application/problem+json", body: JSON.stringify({ type, title: "Refused", status, detail }) })
      : route.fallback();
  await page.route("**/api/v1/applications?*", refuse(429, "Request was throttled. Expected available in 42 seconds.", "/problems/rate-limited", { "Retry-After": "42" }));
  await page.goto("/applications");
  const alert = page.getByRole("main").getByRole("alert");
  await expect(alert).toContainText("Try again in 42 seconds");
  await expect(alert).not.toContainText("throttled");
  await page.unroute("**/api/v1/applications?*");
  // Softwarden's own 429 sentence is kept as it is.
  const own = "Too many refused API tokens from this address. Expected available in 42 seconds.";
  await page.route("**/api/v1/applications?*", refuse(429, own, "/problems/rate-limited", { "Retry-After": "42" }));
  await page.goto("/applications");
  await expect(page.getByRole("main").getByRole("alert")).toContainText(own);
  await page.unroute("**/api/v1/applications?*");

  await page.route("**/api/v1/renewals*", refuse(403, "You do not have permission to perform this action.", "/problems/forbidden"));
  await page.goto("/renewals");
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Your role can't open or change this.");
  await expect(page.getByRole("main").getByRole("alert")).not.toContainText("You do not have permission");
  await page.context().close();
});

test("Each tab of Reports, Data quality and People & Teams has its own document title", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  for (const [route, page_] of [["/reports", "Reports"], ["/data-quality", "Data quality"], ["/admin/people", "People & Teams"]] as const) {
    await page.goto(route);
    const tabs = page.getByRole("tablist").first().getByRole("tab");
    await expect(tabs.first()).toBeVisible();
    const titles = new Set<string>();
    for (let i = 0; i < Math.min(3, await tabs.count()); i++) {
      const tab = tabs.nth(i);
      const label = (await tab.textContent())!.trim();
      await tab.click();
      await expect(page).toHaveTitle(`${label} · ${page_} · Softwarden`);
      titles.add(await page.title());
    }
    expect(titles.size, `${route}: one title per tab`).toBeGreaterThan(1);
  }
  await page.context().close();
});

test("Vendors live at /vendors; the old People & Teams address redirects and keeps its parameters", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "viewer@example.test");
  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  // The g v shortcut goes there too.
  await page.keyboard.press("g");
  await page.keyboard.press("v");
  await expect(page).toHaveURL(/\/vendors$/);
  await page.goto("/dashboard");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Vendors", exact: true }).click();
  await expect(page).toHaveURL(/\/vendors$/);
  await expect(page.getByRole("heading", { level: 1, name: "Vendors" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Vendors table" })).toContainText("Used by");
  await expect(page).toHaveTitle("Vendors · Softwarden");
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Vendors", exact: true })).toHaveAttribute("aria-current", "page");

  // An old link (a bookmark, an email) still works, with its other parameters.
  await page.goto("/admin/people?tab=vendors&attachments=00000000-0000-4000-8000-000000000001#files");
  await expect(page).toHaveURL(/\/vendors\?attachments=00000000-0000-4000-8000-000000000001#files$/);
  await expect(page.getByRole("heading", { level: 1, name: "Vendors" })).toBeVisible();
  // People & Teams keeps its own three tabs.
  await page.goto("/admin/people");
  await expect(page.getByRole("tablist", { name: "People & Teams" }).getByRole("tab")).toHaveText(["People", "Teams", "Departments"]);
  await page.context().close();
});

test("An upload or download answered with a web page says so instead of acting on it", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const html = { status: 200, contentType: "text/html", body: "<html><body>Sign in to the corporate gateway</body></html>" };
  await page.route("**/api/v1/imports", (route) => (route.request().method() === "POST" ? route.fulfill(html) : route.fallback()));
  await page.goto("/imports");
  await page.getByRole("link", { name: "New import" }).first().click();
  await page.getByLabel("CSV file").setInputFiles({ name: "apps.csv", mimeType: "text/csv", buffer: Buffer.from("name,lifecycle\nOne,active\n") });
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(page.getByRole("main")).toContainText("The server's answer wasn't readable.");
  await expect(page).toHaveURL(/\/imports\/new$/);

  await page.route("**/api/v1/contracts/export.csv*", (route) => route.fulfill(html));
  await page.goto("/contracts");
  await page.getByRole("button", { name: "Export CSV" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("something in front of Softwarden");
  await page.context().close();
});

test("A zero-decimal currency shows no .00; a two-decimal one keeps its cents", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const yen = await post(page, "contracts", { name: `${t} Tokyo office suite`, amount: "1500000.00", currency: "JPY", billing_frequency: "annual" });
  await post(page, "contracts", { name: `${t} Dublin office suite`, amount: "1200.00", currency: "EUR", billing_frequency: "annual" });
  await page.goto(`/contracts?q=${t}`);
  const table = page.getByRole("region", { name: "Contracts table" });
  await expect(table.getByRole("row", { name: new RegExp(`${t} Tokyo`) })).toContainText("1,500,000 JPY");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Tokyo`) })).not.toContainText("1,500,000.00");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Dublin`) })).toContainText("1,200.00 EUR");
  await page.goto(`/contracts/${yen.id}`);
  await expect(page.getByRole("main")).toContainText("1,500,000 JPY");
  await page.context().close();
});

test("The dashboard's spend chart labels a JPY bar as its table does", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  // One JPY group, answered here, so the chart has a known bar whatever else the organization spends.
  await page.route("**/api/v1/dashboard", async (route) => {
    const body = await (await route.fetch()).json();
    const group = { key: "v1", label: "Tokyo Software KK", gap: null, currency: "JPY", amount: "1500000.00", contracts: 1, records: 1 };
    const total = { currency: "JPY", annualized: "1500000.00", run_rate: "1500000.00", one_time: "0.00", future: "0.00", future_one_time: "0.00", next_start: null, contracts: 1 };
    body.spend = { totals: [total], by_vendor: [group], by_department: [group], by_category: [group] };
    await route.fulfill({ json: body });
  });
  await page.goto("/dashboard");
  const chart = page.getByTestId("spend-chart");
  await expect(chart).toContainText("1,500,000");
  await expect(chart).not.toContainText("1,500,000.00");
  await page.context().close();
});

test("Deactivating says sessions end now and tokens are only refused while inactive", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  // The seeded accounts are every spec's: answer the change here instead of deactivating one for real.
  await page.route("**/api/v1/users/*", (route) => (route.request().method() === "PATCH"
    ? route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
    : route.fallback()));
  await page.goto("/admin/users");
  await expect(page.getByTestId("deactivate-help")).toContainText("Their API tokens are refused while they are inactive and work again on reactivation");
  await page.getByRole("button", { name: /^Deactivate Sam Editor/ }).click();
  const note = page.getByRole("status").filter({ hasText: /is deactivated and their sessions have ended/ });
  await expect(note).toHaveCount(1);
  await expect(note).toContainText("revoke its tokens in API tokens");
  await page.context().close();
});

test("A moved Halo address or SMTP server asks for its secret again", async ({ browser, baseURL }) => {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const { email } = await post(contoso, "e2e/enterprise-org", { which: "secondary" });
  await contoso.context().close();
  const page = await pageAs(browser, baseURL, email);
  const t = token();
  await post(page, "connections/halo", { name: `${t} Halo`, base_url: "https://halo.simulated.invalid", client_id: "client-1", secret: "synthetic-secret-1" });
  let patched = 0;
  await page.route("**/api/v1/connections/halo/*", (route) => { if (route.request().method() === "PATCH") patched += 1; return route.fallback(); });
  await page.goto("/discovery/connections?tab=halo");
  await page.getByRole("button", { name: `Edit ${t} Halo` }).click();
  const dialog = page.getByRole("dialog", { name: `Edit ${t} Halo` });
  await expect(dialog.getByRole("button", { name: "Save connection" })).toBeEnabled();
  await expect(dialog.getByLabel("Replace client secret")).toBeVisible();
  await dialog.getByLabel("Halo address (required)").fill("https://other.simulated.invalid");
  const secret = dialog.getByLabel("New client secret (required)");
  await expect(secret).toBeVisible();
  await expect(dialog).toContainText("The address changed, so the stored client secret isn't sent there");
  await expect(dialog).not.toContainText("Leave empty to keep the stored client secret");
  await dialog.getByRole("button", { name: "Save connection" }).click();
  await expect(dialog).toContainText("The address changed: enter the client secret for the new address.");
  expect(patched).toBe(0);
  await page.keyboard.press("Escape");

  // Okta (a preview source): the same rule, on a connection answered here.
  const okta = {
    id: "00000000-0000-4000-8000-00000000a0a0", name: `${t} Okta`, enabled: true, notes: "", client_id: "0oa1", simulated: false,
    secret_set_on: "2026-11-01", revision: 1, tested_revision: 1, test_current: true, last_test: null, connectors: [],
    created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z", base_url: "https://acme.okta.com", scope: "okta.apps.read", key_id: "",
  };
  await page.route("**/api/v1/auth/me", async (route) => {
    const me = await (await route.fetch()).json();
    await route.fulfill({ json: { ...me, capabilities: [...new Set([...(me.capabilities as string[]), "preview", "discovery.okta"])] } });
  });
  let oktaPatched = 0;
  await page.route((url) => url.pathname.startsWith("/api/v1/connections/okta"), (route) => {
    if (route.request().method() === "PATCH") { oktaPatched += 1; return route.fulfill({ status: 500, json: {} }); }
    const one = new URL(route.request().url()).pathname.endsWith(okta.id);
    return route.fulfill({ json: one ? okta : [okta], headers: { ETag: '"r1"' } });
  });
  await page.goto("/discovery/connections?tab=okta");
  await page.getByRole("button", { name: `Edit ${t} Okta` }).click();
  const oktaDialog = page.getByRole("dialog", { name: `Edit ${t} Okta` });
  await expect(oktaDialog.getByRole("button", { name: "Save connection" })).toBeEnabled();
  await oktaDialog.getByLabel("Okta address (required)").fill("https://evil.example.test");
  await expect(oktaDialog.getByLabel("New private key (required)")).toBeVisible();
  await oktaDialog.getByRole("button", { name: "Save connection" }).click();
  await expect(oktaDialog).toContainText("The address changed: enter the private key for the new address.");
  expect(oktaPatched).toBe(0);
  await page.keyboard.press("Escape");
  await page.unroute("**/api/v1/auth/me");

  // SMTP, with a stored password (the settings are answered here, so nothing is sent anywhere).
  await page.route("**/api/v1/notification-settings/email", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const res = await route.fetch();
    const body = { ...(await res.json()), transport: "smtp", smtp_host: "mail.example.test", smtp_port: null, smtp_security: "starttls", smtp_password_set: true, saved: true };
    await route.fulfill({ response: res, json: body });
  });
  await page.goto("/admin/notification-channels");
  await expect(page.getByLabel("Replace password")).toBeVisible();
  await page.getByLabel("Host").fill("smtp.elsewhere.test");
  await expect(page.getByLabel("New password (required, or remove it)")).toBeVisible();
  await expect(page.getByRole("main")).toContainText("The SMTP server changed, so the stored password isn't sent there");

  // Leaving SMTP for Graph after editing the host: the SMTP edits aren't sent (the API would refuse the moved server
  // on a field no longer shown). The save is answered here.
  const sent: Record<string, unknown>[] = [];
  await page.route("**/api/v1/notification-settings/email", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    sent.push(route.request().postDataJSON());
    await route.fulfill({ status: 200, json: {} });
  });
  // Still on SMTP: Save is refused in the form, with the reason on the password field, and no PATCH goes out.
  await page.getByRole("button", { name: "Save email settings" }).click();
  await expect(page.locator("#smtp_password-error")).toContainText("The SMTP server changed");
  expect(sent).toEqual([]);
  await page.getByRole("radio", { name: "Microsoft Graph (sendMail)" }).check();
  await page.getByRole("button", { name: "Save email settings" }).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(Object.keys(sent[0]).filter((k) => k.startsWith("smtp_"))).toEqual([]);
  expect(sent[0].transport).toBe("graph");
  await page.context().close();
});

test("The System page links to the third-party notices the image serves", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.goto("/admin/system");
  // Generated at image build: a development server may answer 404, so only the link is checked here.
  await expect(page.getByTestId("third-party-notices").getByRole("link", { name: "Third-party notices" })).toHaveAttribute("href", "/THIRD_PARTY_NOTICES.txt");
  await page.context().close();
});

test("Shadow IT, SSO coverage, ticket and discovery connections name no capability", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.route("**/api/v1/auth/me", async (route) => {
    const body = await (await route.fetch()).json();
    const caps = (body.capabilities as string[]).filter((c) => !c.startsWith("discovery."));
    await route.fulfill({ json: { ...body, capabilities: [...new Set([...caps, "preview"])] } });
  });
  for (const [route, text] of [
    ["/discovery/shadow-it", "Shadow IT isn't available."], ["/discovery/sso-coverage", "SSO coverage isn't available."],
    ["/admin/ticket-connections", "Halo ticket links isn't available."], ["/discovery/connections?tab=halo", "Halo connections aren't available."],
  ] as const) {
    await page.goto(route);
    const notice = page.getByRole("main").getByText(text).locator("xpath=..");
    await expect(notice).toBeVisible();
    await expect(notice, route).not.toContainText("capability");
    await expect(notice, route).not.toContainText("discovery.");
  }
  await page.context().close();
});

test("Catalog as code's notice names the edition, not the capability", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  // As an organization whose catalog_code capability was turned off (a platform override).
  await page.route("**/api/v1/auth/me", async (route) => {
    const body = await (await route.fetch()).json();
    await route.fulfill({ json: { ...body, capabilities: (body.capabilities as string[]).filter((c) => c !== "catalog_code") } });
  });
  await page.goto("/admin/catalog-code");
  const notice = page.locator("[data-unavailable]");
  await expect(notice).toContainText("It is part of every edition");
  await expect(notice).not.toContainText("catalog_code");
  await expect(notice).not.toContainText("capability");
  await page.context().close();
});

test("The sign-in page welcomes invited members instead of turning owners away", async ({ browser, baseURL }) => {
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto("/login");
  const card = page.getByRole("main");
  await expect(card).toContainText("including anyone an Admin invited");
  await expect(card).not.toContainText("IT staff accounts only");
  await expect(card).not.toContainText("Owners don't need to sign in");
  await page.context().close();
});
