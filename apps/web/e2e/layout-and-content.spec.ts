import { expect, type Locator, type Page, test } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** Layout: a deadline's explanation wraps inside its cell instead of printing over
 *  the next column (1440 and 1024 px), and no two chips sit closer than 4 px, on Needs attention and Applications. */

test.describe.configure({ timeout: 120_000 });

const U = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const day = (offset: number) => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);

/** Deadline rows with explanations as long as the real ones (dates and numbers in the sentence). */
const DEADLINES = Array.from({ length: 4 }, (_, i) => ({
  uid: `contract-${U(i + 1)}@softwarden`, source: i % 2 ? "contract" : "renewal", id: U(i + 1), name: `Fixture deadline ${i + 1}`,
  vendor: "Contoso", date: "2026-11-20", action: i % 2 ? "renew_by" : "cancel_by", label: "calculated", label_text: "Calculated",
  explanation: `Renewal date 2026-12-2${i} minus notice period 30 days = 2026-11-2${i}, then the organization's 14 day lead time.`,
  term_ends: "2026-12-20", auto_renew: "yes", decision: null, timing: { days: 10, overdue: false, text: "in 10 days" },
  contract: null, annualized_cost: "1200.00", currency: "USD", owner: null, path: `/contracts/${U(i + 1)}`,
}));

async function withDeadlines(page: Page) {
  await page.route("**/api/v1/dashboard", async (route) => {
    const body = await (await route.fetch()).json();
    body.deadlines = { overdue: 0, total: DEADLINES.length, windows: [{ days: 30, count: DEADLINES.length }, { days: 60, count: DEADLINES.length }, { days: 90, count: DEADLINES.length }], rows: DEADLINES };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
}

for (const width of [1440, 1024]) {
  test(`Deadline explanations stay inside their cell at ${width} px`, async ({ browser, baseURL }) => {
    const page = await pageAs(browser, baseURL, "admin@example.test");
    await page.setViewportSize({ width, height: 900 });
    await withDeadlines(page);
    await page.goto("/dashboard");
    const lines = page.getByTestId("dash-deadlines").getByTestId("deadline-explanation");
    await expect(lines).toHaveCount(DEADLINES.length);
    for (const line of await lines.all()) {
      const { text, cell } = await line.evaluate((el) => {
        const td = el.closest("td")!;
        const t = el.getBoundingClientRect();
        const c = td.getBoundingClientRect();
        const padRight = parseFloat(getComputedStyle(td).paddingRight);
        return { text: { left: t.left, right: t.right }, cell: { left: c.left, right: c.right - padRight } };
      });
      expect(text.left).toBeGreaterThanOrEqual(cell.left);
      expect(text.right).toBeLessThanOrEqual(cell.right + 0.5);
    }
    // The explanation wraps rather than widening past its max width.
    expect(await lines.first().evaluate((el) => el.getClientRects().length > 0 && el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.context().close();
  });
}

/** The smallest distance between any two chips inside `scope` (on either axis; overlapping boxes count as negative). */
async function closestChips(scope: Locator) {
  return scope.evaluate((root) => {
    const els = [...root.querySelectorAll(".sw-chip")].filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    const boxes = els.map((el) => el.getBoundingClientRect());
    let min = Infinity;
    let pair = "";
    const desc = (k: number) => {
      const el = els[k], r = boxes[k];
      const cell = el.closest("td, th");
      return `"${el.textContent}" [${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.width)}x${Math.round(r.height)}] in ${cell ? `${cell.tagName.toLowerCase()} of "${cell.closest("tr")?.textContent?.slice(0, 60)}"` : el.parentElement?.className}`;
    };
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        const dx = Math.max(b.left - a.right, a.left - b.right);
        const dy = Math.max(b.top - a.bottom, a.top - b.bottom);
        const d = Math.max(dx, dy);
        if (d < min) { min = d; pair = `${i}/${j}: ${desc(i)} and ${desc(j)}`; }
      }
    }
    return { min, pair, count: boxes.length };
  });
}

for (const width of [1440, 375]) {
  test(`Chips keep at least 4 px apart on Needs attention and Applications at ${width} px`, async ({ browser, baseURL }) => {
    const page = await pageAs(browser, baseURL, "admin@example.test");
    await page.setViewportSize({ width, height: 900 });
    const t = token();
    // No owners and no support team: "No business owner", "No technical owner", "No support team" side by side. And a
    // long tag: at 375 px the name column is 144 px wide, and a 22-character tag chip used to run 2 px into the
    // Lifecycle chip beside it. The suite's other specs make tags like this, and the list's search can show their rows
    // too, which is why this failed only in full runs; the tag here makes the case certain.
    await post(page, "applications", { name: `${t} Chips`, lifecycle: "active", tags: [`${t}-new`] });
    // No renewal date and no IT follow-up: two renewal gaps side by side.
    await post(page, "renewals", { name: `${t} Gappy renewal`, auto_renew: "unknown", explicit_deadline: day(20) });

    await page.goto("/?scope=all");
    await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
    const owners = page.locator("tr", { hasText: `${t} Chips` }).first();
    await expect(owners.getByText("No business owner")).toBeVisible();
    await expect(owners.getByText("No technical owner")).toBeVisible();
    const attention = await closestChips(page.getByRole("main"));
    expect(attention.count).toBeGreaterThan(2);
    expect(attention.min, `closest chips ${attention.pair}`).toBeGreaterThanOrEqual(4);

    await page.goto(`/applications?q=${t}`);
    const row = page.getByRole("region", { name: "Applications table" }).getByRole("row", { name: new RegExp(`${t} Chips`) });
    await expect(row.getByText("No business owner")).toBeVisible();
    await expect(row.getByText("No technical owner")).toBeVisible();
    const list = await closestChips(page.getByRole("main"));
    expect(list.count).toBeGreaterThan(2);
    expect(list.min, `closest chips ${list.pair}`).toBeGreaterThanOrEqual(4);
    await page.context().close();
  });
}

/** Content: a deadline with no renewal date says so under its action. */
test("A deadline row without a renewal date shows the gap under its action", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "viewer@example.test");
  await page.route("**/api/v1/dashboard", async (route) => {
    const body = await (await route.fetch()).json();
    body.deadlines = { overdue: 0, total: 1, windows: [{ days: 30, count: 1 }, { days: 60, count: 1 }, { days: 90, count: 1 }], rows: [{ ...DEADLINES[0], term_ends: null, name: "Payroll connector license" }] };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/dashboard");
  const row = page.getByTestId("dash-deadlines").getByRole("row", { name: /Payroll connector license/ });
  await expect(row.getByRole("cell").nth(1)).toContainText("Cancel by");
  await expect(row.getByRole("cell").nth(1).locator("[data-gap]")).toHaveText("⚠No renewal date");
  await page.context().close();
});

/** An application with no Entra link, vendor, version or authentication method. */
test("The record page says once that no Entra app is linked, and blank details are gaps or Not recorded", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const app = await post(editor, "applications", { name: `${t} Unlinked`, lifecycle: "active", deployment_model: "on_prem" });
  await editor.goto(`/applications/${app.id}`);
  const panel = editor.getByRole("region", { name: "Single sign-on and MFA" });
  await expect(panel.getByTestId("coverage-empty")).toContainText("Not linked to an Entra application");
  await expect(panel.getByText(/unknown/)).toHaveCount(0);
  await expect(panel.getByText("Checked not yet")).toHaveCount(0);
  const details = editor.getByRole("region", { name: "Details" });
  const value = (label: string) => details.locator(`dt:text-is("${label}") + dd`);
  await expect(value("Vendor")).toHaveText("⚠No vendor");
  await expect(value("Authentication method")).toHaveText("⚠No authentication method");
  await expect(value("Version")).toHaveText("Not recorded");
  const linkEntra = panel.getByRole("link", { name: /^Link an Entra application/ });
  await expect(linkEntra).toHaveAttribute("href", `/applications/${app.id}/edit#entra_app_id`);
  await linkEntra.click();
  // The link names the field; the form focuses it, then clears the hash so a remount doesn't move focus again.
  await expect(editor).toHaveURL(new RegExp(`/applications/${app.id}/edit$`));
  await expect(editor.getByLabel("Application (client) ID")).toBeFocused();
  await editor.context().close();

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${app.id}`);
  const read = viewer.getByRole("region", { name: "Single sign-on and MFA" });
  await expect(read.getByTestId("coverage-empty")).toContainText("Editors and Admins can link an Entra application.");
  await expect(read.getByRole("link")).toHaveCount(0);
  await viewer.context().close();
});

/** The email warning names what the reader can do: the server variable when self-hosted, support when hosted. */
test("The email warning on Organization settings depends on the deployment", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.route("**/api/v1/organization", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const body = await (await route.fetch()).json();
    body.email = { ...body.email, configured: false };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/admin/organization");
  await expect(page.getByTestId("email-unconfigured")).toContainText("Set SOFTWARDEN_SMTP_HOST");
  // Where the settings are documented, as the API's own message says (docs/ENV.md), not the README.
  await expect(page.getByTestId("email-unconfigured")).toContainText("(docs/ENV.md)");

  // The hosted service: auth/me says the deployment is cloud.
  await page.route("**/api/v1/auth/me", async (route) => {
    const body = await (await route.fetch()).json();
    body.deployment = "cloud";
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.reload();
  const note = page.getByTestId("email-unconfigured");
  await expect(note).toContainText("The hosted email service isn't configured for this organization.");
  await expect(note).toContainText("Contact support");
  await expect(note).not.toContainText("SOFTWARDEN_SMTP_HOST");
  await page.context().close();
});

/** The main area fills the window beside the sidebar (owner report 2026-11-10): no 1280 px cap leaving an empty band. */
test("Main uses the full window width beside the sidebar at 1920 px", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.setViewportSize({ width: 1920, height: 1000 });
  for (const path of ["/dashboard", "/applications"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const { main, viewport, sidebar } = await page.evaluate(() => ({
      main: document.getElementById("main")!.getBoundingClientRect().width,
      viewport: document.documentElement.clientWidth,
      sidebar: document.querySelector(".bg-sidebar")!.getBoundingClientRect().width,
    }));
    expect(sidebar).toBeGreaterThan(0);
    expect(main, `main on ${path}`).toBeGreaterThanOrEqual(viewport - sidebar - 2);
  }
  await page.context().close();
});
