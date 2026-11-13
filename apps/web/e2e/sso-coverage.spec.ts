import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post } from "./helpers";

/** SSO and MFA coverage. The `entra_coverage` source's setup card, its three permissions and its scope
 *  fields; the coverage report with each dimension as glyph plus words, its filters and sort in the URL and CSV; an
 *  application's coverage panel with its policies and "Source differs"; a Viewer's read-only report and panel, the
 *  export turned off while impersonating; and the three data-quality rules. axe in both themes.
 *
 *  The coverage connector and its applications are organization-wide, so the spec runs in its own organization
 *  (`e2e/spec-org`, `sso_coverage`): seven live applications, six matched by entra_app_id to the simulated tenant's apps
 *  (api/e2e_views.py, sso_coverage_scenario). A tested connection on the simulated tenant behind a coverage connector,
 *  run once as the worker would. Nothing contacts Microsoft. */

const ADMIN = "sc-admin@example.test";
const EDITOR = "sc-editor@example.test";
const VIEWER = "sc-viewer@example.test";
const CONNECTOR = "Tailspin coverage";

test.describe.configure({ mode: "serial", timeout: 180_000 });

let connectorId = "";
let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "sso_coverage" });
    await contoso.context().close();
    const admin = await pageAs(browser, baseURL, ADMIN);
    const existing: { id: string; name: string; source: string; last_run: unknown }[] = await (await admin.request.get("/api/v1/connectors")).json();
    const found = existing.find((c) => c.source === "entra_coverage" && c.name === CONNECTOR);
    if (found) connectorId = found.id;
    else {
      const conn = await post(admin, "connections/microsoft", { name: `${CONNECTOR} tenant`, tenant_id: "simulated", secret: "synthetic-secret" });
      await post(admin, `connections/microsoft/${conn.id}/test`, {});
      connectorId = (await post(admin, "connectors", { source: "entra_coverage", name: CONNECTOR, microsoft_connection: conn.id })).id;
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

const table = (page: Page) => page.getByRole("region", { name: "SSO coverage table" });
const row = (page: Page, name: string) => table(page).getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });

test("An Admin finds the coverage source in the setup with its three permissions and its scope", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/discovery/setup");
  const card = page.locator("[data-source=entra_coverage]");
  await expect(card.getByRole("heading", { name: "Microsoft Entra SSO and MFA coverage" })).toBeVisible();
  await expect(card.getByRole("link", { name: CONNECTOR })).toBeVisible();
  await card.getByRole("link", { name: "Set up Microsoft Entra SSO and MFA coverage" }).click();
  await expect(page.getByRole("heading", { name: "Prepare", exact: true })).toBeVisible();
  const permissions = page.getByRole("region", { name: "Permissions table" });
  for (const p of ["Application.Read.All", "Synchronization.Read.All", "Policy.Read.All"]) {
    await expect(permissions.getByRole("rowheader", { name: new RegExp(p.replace(/\./g, "\\.")) })).toBeVisible();
  }
  await expect(permissions.getByRole("row")).toHaveCount(4);
  await expect(page.getByRole("link", { name: "permissions reference, Microsoft Entra SSO and MFA coverage" })).toHaveAttribute("href", "/discovery/permissions#discovery-source-entra-coverage");
  await expectNoViolations(page, "Coverage walkthrough, Prepare");

  // The connector's own page: its scope's two fields.
  await page.goto(`/discovery/connectors/${connectorId}`);
  await expect(page.getByRole("heading", { level: 1, name: CONNECTOR })).toBeVisible();
  await expect(page.getByLabel("Read provisioning (SCIM) jobs")).toBeVisible();
  await expect(page.getByLabel("Read conditional access policies")).toBeVisible();
  await page.context().close();
});

test("An Editor reads the report, filters and sorts it in the URL, exports it and opens a record's panel", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  await page.goto("/discovery");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "SSO coverage" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "SSO coverage" })).toBeVisible();
  for (const name of ["Workday", "Zoom", "ServiceNow", "Expense Hub", "Finance Ledger", "Contract Vault", "Legacy Intranet"]) await expect(row(page, name)).toHaveCount(1);
  // Each dimension: glyph plus words, or a named gap.
  await expect(row(page, "Workday").getByTestId("cell-sso")).toContainText("✓SAML");
  await expect(row(page, "Workday").getByTestId("cell-scim")).toContainText("✓SCIM on");
  await expect(row(page, "Workday").getByTestId("cell-mfa")).toContainText("✓MFA for all users");
  await expect(row(page, "Zoom").getByTestId("cell-scim").locator("[data-gap]")).toHaveText("⚠No SCIM");
  await expect(row(page, "Zoom").getByTestId("cell-mfa").locator("[data-gap]")).toHaveText("⚠No MFA policy");
  await expect(row(page, "Finance Ledger").getByTestId("cell-mfa").locator("[data-gap]")).toHaveText("⚠MFA for some users only");
  await expect(row(page, "Contract Vault").getByTestId("cell-sso").locator("[data-gap]")).toHaveText("⚠No SSO");
  // Unknown is a named gap with its reason.
  await expect(row(page, "ServiceNow").getByTestId("cell-sso").locator("[data-gap]")).toHaveText("⚠SSO unknown");
  await expect(row(page, "Legacy Intranet").getByTestId("cell-sso")).toContainText("Not linked to an Entra application");
  await expect(row(page, "Legacy Intranet")).toContainText("Not linked to an Entra app");
  // Where the record says otherwise: shown, never applied.
  await expect(row(page, "Workday").getByTestId("row-differs")).toContainText("Source differs");
  await expect(row(page, "Workday").getByTestId("row-differs")).toContainText("SSO enabled: Yes in Entra, no here");
  await expect(row(page, "Zoom").getByTestId("row-differs")).toContainText("SCIM provisioning: No in Entra, yes here");
  // Most critical first.
  await expect(table(page).getByRole("columnheader", { name: /Criticality/ })).toHaveAttribute("aria-sort", "ascending");
  await expectNoViolations(page, "SSO coverage report");

  // Filters live in the URL and survive a reload.
  await page.getByLabel("Gap").selectOption("no_mfa");
  await expect(page).toHaveURL(/gap=no_mfa/);
  await expect(row(page, "Zoom")).toHaveCount(1);
  await expect(row(page, "Workday")).toHaveCount(0);
  await page.getByLabel("Criticality").selectOption("high");
  await expect(page).toHaveURL(/criticality=high/);
  await page.reload();
  await expect(page.getByLabel("Gap")).toHaveValue("no_mfa");
  await expect(page.getByLabel("Criticality")).toHaveValue("high");
  await expect(row(page, "Zoom")).toHaveCount(1);
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/discovery\/sso-coverage$/);
  await page.getByLabel("Single sign-on").selectOption("unknown");
  await expect(row(page, "ServiceNow")).toHaveCount(1);
  await expect(row(page, "Legacy Intranet")).toHaveCount(1);
  await expect(row(page, "Workday")).toHaveCount(0);
  await page.getByLabel("Entra app").selectOption("true");
  await expect(page).toHaveURL(/linked=true/);
  await expect(row(page, "Legacy Intranet")).toHaveCount(0);
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await page.getByLabel("Search").fill("expense");
  await page.getByLabel("Search").press("Enter");
  await expect(page).toHaveURL(/q=expense/);
  await expect(row(page, "Expense Hub")).toHaveCount(1);
  await expect(row(page, "Workday")).toHaveCount(0);
  await page.getByRole("button", { name: "Clear filters" }).first().click();

  // Sort by MFA from its header: worst first, the order in the URL.
  const mfa = table(page).getByRole("columnheader", { name: /^MFA/ });
  await mfa.getByRole("button").click();
  await expect(page).toHaveURL(/ordering=mfa/);
  await expect(mfa).toHaveAttribute("aria-sort", "ascending");
  await expect(table(page).getByRole("rowheader").first()).toContainText(/Zoom|ServiceNow/);
  await page.getByRole("button", { name: "Clear filters" }).first().click();

  // CSV export through the shared download helper.
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/^softwarden-sso-coverage-\d{4}-\d{2}-\d{2}\.csv$/);
  await expect(page.getByRole("status").filter({ hasText: "Exported the SSO coverage report to" })).toHaveCount(1);

  // The record's coverage panel.
  await row(page, "Workday").getByRole("link", { name: "Workday" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Workday" })).toBeVisible();
  const panel = page.getByRole("region", { name: "Single sign-on and MFA" });
  await expect(panel.getByTestId("panel-sso")).toHaveText("✓SAML");
  await expect(panel.getByTestId("panel-scim")).toHaveText("✓SCIM on");
  await expect(panel.getByTestId("panel-mfa")).toHaveText("✓MFA for all users");
  await expect(panel.getByTestId("coverage-gaps")).toContainText("No coverage gaps");
  await expect(panel).toContainText("Matched by its Entra application ID, read by Tailspin coverage");
  await expect(panel).toContainText(/Checked \d{4}-\d{2}-\d{2}/);
  await expect(panel.locator('[data-policy="Require MFA for Workday"]')).toContainText("Requires MFA for all users");
  await expect(panel.getByTestId("coverage-differs")).toContainText("“Yes” in Entra; “No” here");
  await expect(panel.getByRole("link", { name: "Edit Workday to apply a difference" })).toHaveAttribute("href", /\/applications\/.+\/edit$/);
  await expectNoViolations(page, "Application overview with coverage");

  // A record with gaps and a report-only policy.
  await page.goto("/discovery/sso-coverage?q=zoom");
  await row(page, "Zoom").getByRole("link", { name: "Zoom" }).click();
  const zoom = page.getByRole("region", { name: "Single sign-on and MFA" });
  await expect(zoom.getByTestId("coverage-gaps").locator("[data-gap]")).toContainText(["No SCIM", "No MFA policy"]);
  await expect(zoom.locator('[data-policy="Report-only: MFA for Zoom"]')).toContainText("Report-only, not enforced");
  await page.context().close();
});

test("A Viewer reads the report and a record's panel, and can't export while impersonated", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  await page.goto("/discovery/sso-coverage");
  await expect(page.getByRole("heading", { level: 1, name: "SSO coverage" })).toBeVisible();
  await expect(row(page, "Workday")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Export CSV" })).toBeVisible();
  await expectNoViolations(page, "SSO coverage report (Viewer)");
  await row(page, "Contract Vault").getByRole("link", { name: "Contract Vault" }).click();
  const panel = page.getByRole("region", { name: "Single sign-on and MFA" });
  await expect(panel.getByTestId("panel-mfa").locator("[data-gap]")).toHaveText("⚠MFA unknown");
  await expect(panel.getByRole("link")).toHaveCount(0);
  await page.goto("/discovery/sso-coverage?q=workday");
  await row(page, "Workday").getByRole("link", { name: "Workday" }).click();
  await expect(page.getByRole("region", { name: "Single sign-on and MFA" }).getByTestId("coverage-differs")).toContainText("Editors and Admins can edit the record to apply a difference.");
  await expectNoViolations(page, "Application overview with coverage (Viewer)");

  // As `auth/me` says while platform staff act as this member: the export gives way to a sentence.
  await page.route("**/api/v1/auth/me", async (route) => {
    const body = await (await route.fetch()).json();
    const now = Date.now();
    await route.fulfill({ json: { ...body, impersonation: {
      id: "00000000-0000-4000-8000-000000000001", by: { id: 999, name: "Platform Support", email: "support@example.test" }, reason: "Ticket 42 (synthetic)",
      started_at: new Date(now - 60_000).toISOString(), expires_at: new Date(now + 3_600_000).toISOString(),
    } } });
  });
  await page.goto("/discovery/sso-coverage");
  await expect(page.getByRole("heading", { level: 1, name: "SSO coverage" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Export CSV" })).toHaveCount(0);
  await expect(page.getByTestId("export-off")).toHaveText("⊘ CSV export is off while you act as this member.");
  await page.context().close();
});

test("Data quality lists the three coverage rules and the gaps they find", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/data-quality");
  const zoom = page.getByRole("row").filter({ hasText: "Zoom" });
  for (const gap of ["No SCIM", "No MFA policy"]) await expect(zoom.locator("[data-gap]").filter({ hasText: gap })).toHaveCount(1);
  await expect(zoom.locator("[data-gap]").filter({ hasText: "No SSO" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Checks" }).click();
  const rules = page.getByRole("region", { name: "Data-quality checks table" });
  await expect(rules.locator('[data-rule="sso_missing"]')).toContainText("No SSO");
  await expect(rules.locator('[data-rule="mfa_missing"]')).toContainText("No MFA policy");
  await expect(rules.locator('[data-rule="scim_missing"]')).toContainText("No SCIM");
  await expectNoViolations(page, "Data quality checks with coverage rules");
  await page.context().close();
});
