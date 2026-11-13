import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Shadow IT. The `entra_shadow_it` source's setup and access check on the simulated tenant; the overview
 *  with its privacy note, filters and sort in the URL; an Editor creating a record from a discovered app with the
 *  discovery queue's dialog; a Viewer's read-only overview; axe in both themes.
 *
 *  Connectors and their candidates are organization-wide (discovery.spec.ts expects Contoso to start with none), so the
 *  spec runs in its own organization (`e2e/spec-org`, `shadow_it`), its tests in order. The simulated tenant's shadow IT
 *  apps: MeetNote AI (user consent by three people, Mail.ReadWrite and offline_access), FileBridge Sync (admin consent,
 *  Files.ReadWrite.All), ChartPilot (sign-ins, no consent) and QuickPoll (one person's consent, Directory.Read.All).
 *  Nothing contacts Microsoft.
 *
 *  The Editor test creates a record from ChartPilot, which only works while ChartPilot is still to decide: the e2e
 *  database is dropped and recreated on every Playwright run (apps/api/e2e_db.py), so each run starts with it new. The
 *  connector setup reuses what it finds, so a retry within a run gets past it; the create itself is not repeatable. */

const ADMIN = "si-admin@example.test";
const EDITOR = "si-editor@example.test";
const VIEWER = "si-viewer@example.test";

test.describe.configure({ mode: "serial", timeout: 180_000 });

const t = token();
const CONNECTOR = "Trey Research shadow IT";
let connectorId = "";
let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "shadow_it" });
    await contoso.context().close();
    // A tested connection on the simulated tenant behind a shadow IT connector, run once, as the worker would. Made
    // once per database: a rerun against the same one (a retry, a second local run) reuses them.
    const admin = await pageAs(browser, baseURL, ADMIN);
    const existing: { id: string; name: string; source: string; last_run: unknown }[] = await (await admin.request.get("/api/v1/connectors")).json();
    const found = existing.find((c) => c.source === "entra_shadow_it" && c.name === CONNECTOR);
    if (found) connectorId = found.id;
    else {
      const conn = await post(admin, "connections/microsoft", { name: `${CONNECTOR} tenant`, tenant_id: "simulated", secret: "synthetic-secret" });
      await post(admin, `connections/microsoft/${conn.id}/test`, {});
      connectorId = (await post(admin, "connectors", { source: "entra_shadow_it", name: CONNECTOR, microsoft_connection: conn.id })).id;
    }
    if (!found?.last_run) {
      await post(admin, `connectors/${connectorId}/run`, {});
      const ran: { connector: string; status: string }[] = await post(admin, "e2e/run-connectors", {});
      expect(ran.find((r) => r.connector === connectorId)?.status).toBe("succeeded");
    }
    await admin.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const table = (page: Page) => page.getByRole("region", { name: "Shadow IT table" });
const row = (page: Page, name: string) => table(page).getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });

test("An Admin finds the shadow IT source in the setup, and its access check passes", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/discovery/setup");
  const card = page.locator("[data-source=entra_shadow_it]");
  await expect(card.getByRole("heading", { name: "Microsoft Entra shadow IT" })).toBeVisible();
  await expect(card.getByRole("link", { name: CONNECTOR })).toBeVisible();
  await card.getByRole("link", { name: "Set up Microsoft Entra shadow IT" }).click();
  await expect(page.getByRole("heading", { name: "Prepare", exact: true })).toBeVisible();
  await expect(page.getByText(/AuditLog\.Read\.All \(sign-in logs/)).toBeVisible();
  await expect(page.getByRole("link", { name: "permissions reference, Microsoft Entra shadow IT" })).toHaveAttribute("href", "/discovery/permissions#discovery-source-entra-shadow-it");
  await expectNoViolations(page, "Shadow IT walkthrough, Prepare");

  // The connector's own page: its scope's fields in words, and an access check naming no missing permission.
  await page.goto(`/discovery/connectors/${connectorId}`);
  await expect(page.getByRole("heading", { level: 1, name: CONNECTOR })).toBeVisible();
  await expect(page.getByLabel("Read the sign-in logs")).toBeChecked();
  await page.getByRole("button", { name: /^Check access/ }).click();
  await expect(page.getByTestId("access-check")).toContainText("Every required permission works.");
  await expectNoViolations(page, "Shadow IT connector settings");
  await page.context().close();
});

test("An Editor filters the overview in the URL and creates a record from a discovered app", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  await page.goto("/discovery");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Shadow IT" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Shadow IT" })).toBeVisible();
  await expect(page.getByTestId("privacy-note")).toContainText("Softwarden never stores who signed in or who consented");
  for (const name of ["MeetNote AI", "FileBridge Sync", "ChartPilot", "QuickPoll"]) await expect(row(page, name)).toHaveCount(1);
  await expect(row(page, "MeetNote AI")).toContainText("User consent");
  await expect(row(page, "MeetNote AI")).toContainText("3 people consented");
  await expect(row(page, "MeetNote AI").locator("[data-risky]")).toHaveText("!2 high-risk scopes");
  await expect(row(page, "FileBridge Sync")).toContainText("Admin consent");
  await expect(row(page, "ChartPilot")).toContainText("No consent grant");
  await expect(row(page, "ChartPilot").locator("[data-status]")).toHaveText("○Not in the catalog");
  await expectNoViolations(page, "Shadow IT overview");

  // Filters live in the URL and survive a reload.
  await page.getByLabel("High-risk scopes").selectOption("true");
  await expect(page).toHaveURL(/risky=true/);
  await expect(row(page, "ChartPilot")).toHaveCount(0);
  await expect(row(page, "MeetNote AI")).toHaveCount(1);
  await page.getByLabel("Consent").selectOption("user");
  await expect(page).toHaveURL(/consent=user/);
  await page.reload();
  await expect(page.getByLabel("Consent")).toHaveValue("user");
  await expect(row(page, "FileBridge Sync")).toHaveCount(0);
  await expect(row(page, "MeetNote AI")).toHaveCount(1);
  await expect(row(page, "QuickPoll")).toHaveCount(1);
  await page.getByLabel("Search").fill("quick");
  await page.getByLabel("Search").press("Enter");
  await expect(page).toHaveURL(/q=quick/);
  await expect(row(page, "MeetNote AI")).toHaveCount(0);
  await expect(row(page, "QuickPoll")).toHaveCount(1);
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/discovery\/shadow-it$/);

  // Sort from a header: aria-sort on the th, the order in the URL.
  const signIns = table(page).getByRole("columnheader", { name: /Sign-ins, 30 days/ });
  await expect(table(page).getByRole("columnheader", { name: /Users, 30 days/ })).toHaveAttribute("aria-sort", "descending");
  await signIns.getByRole("button").click();
  await expect(page).toHaveURL(/ordering=-sign_ins_30d/);
  await expect(signIns).toHaveAttribute("aria-sort", "descending");
  await page.getByRole("button", { name: "Clear filters" }).first().click();

  // Create a record from ChartPilot through its discovery candidate.
  await row(page, "ChartPilot").getByRole("button", { name: "Create record for ChartPilot" }).click();
  const create = page.getByRole("dialog", { name: "Create a record from this application" });
  await expect(create.getByLabel("Name")).toHaveValue("ChartPilot");
  await expectNoViolations(page, "Shadow IT create dialog");
  await create.getByLabel("Name").fill(`${t} ChartPilot`);
  await create.getByRole("button", { name: "Create and link" }).click();
  await expect(create).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: `Created ${t} ChartPilot and linked it.` })).toHaveCount(1);
  await expect(row(page, "ChartPilot")).toHaveCount(0);
  await page.getByLabel("Catalog status").selectOption("linked");
  await expect(page).toHaveURL(/status=linked/);
  await expect(row(page, "ChartPilot").locator("[data-status]")).toHaveText("✓Linked");
  await expect(row(page, "ChartPilot").getByRole("link", { name: `${t} ChartPilot` })).toBeVisible();
  await expect(row(page, "ChartPilot").getByRole("button")).toHaveCount(0);
  await page.context().close();
});

test("A Viewer reads the overview and has nothing to decide", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  await page.goto("/discovery/shadow-it?status=all");
  await expect(page.getByRole("heading", { level: 1, name: "Shadow IT" })).toBeVisible();
  await expect(page.getByTestId("read-only")).toHaveText("You can read the overview. Editors and Admins create, link and dismiss apps.");
  await expect(row(page, "MeetNote AI")).toHaveCount(1);
  await expect(table(page).getByRole("button", { name: /^Create record|^Link|^Dismiss/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Export CSV" })).toBeVisible();
  await expectNoViolations(page, "Shadow IT overview (Viewer)");
  await page.context().close();
});
