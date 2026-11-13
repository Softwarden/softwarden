import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post } from "./helpers";

/** Launch scope: the preview features (Okta and Google Workspace discovery, ServiceNow and Halo ticket
 *  connections) are hidden unless the installation sets SOFTWARDEN_PREVIEW_FEATURES, and the e2e server leaves it off,
 *  as by default. Checked as the Admin of an enterprise organization (the compliance one: tickets.spec.ts keeps a Halo
 *  ticket connection in the one for a while), whose edition (like every edition) would otherwise include Okta and Google: they aren't offered anywhere, rather than shown as needing another edition, and the ticket
 *  connections page offers Jira only. axe on each page. Read only: nothing is created. */

test.describe.configure({ timeout: 120_000 });

test("Preview features are hidden: no Okta or Google in the navigation, setup or connections, and Jira only for tickets", async ({ browser, baseURL }) => {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const { email } = await post(contoso, "e2e/enterprise-org", { which: "compliance" });
  await contoso.context().close();
  const page = await pageAs(browser, baseURL, email);

  // What the API reports: no preview capability, and no Okta or Google in any edition.
  const me: { edition: string; capabilities: string[] } = await (await page.request.get("/api/v1/auth/me")).json();
  expect(me.edition).toBe("enterprise");
  expect(me.capabilities).toContain("discovery.halo");
  expect(me.capabilities).toContain("ticket_links");
  for (const name of ["preview", "discovery.okta", "discovery.google"]) expect(me.capabilities).not.toContain(name);

  // The navigation names neither.
  await page.goto("/discovery");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByRole("link", { name: "Connections", exact: true })).toBeVisible();
  await expect(nav).not.toContainText(/Okta|Google/);

  // The guided setup offers no Okta or Google card; the other sources are there.
  await page.goto("/discovery/setup");
  await expect(page.getByRole("link", { name: "Set up HaloITSM" })).toBeVisible();
  await expect(page.locator("[data-source=okta]")).toHaveCount(0);
  await expect(page.locator("[data-source=google_workspace]")).toHaveCount(0);
  await expect(page.getByRole("list", { name: "Sources" })).not.toContainText(/Okta|Google/);
  await expectNoViolations(page, "Choose a source, preview features off");
  // Opened by its address, Okta says why, never offering the setup.
  await page.goto("/discovery/setup/okta");
  await expect(page.getByText("Okta isn't available. It is a preview feature this installation hasn't turned on.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue to Connect" })).toHaveCount(0);

  // Connections: Microsoft and HaloITSM tabs only; an Okta address falls back to Microsoft.
  await page.goto("/discovery/connections?tab=okta");
  const kinds = page.getByRole("tablist", { name: "Connection kinds" });
  await expect(kinds.getByRole("tab")).toHaveText(["Microsoft", "HaloITSM"]);
  await expect(kinds.getByRole("tab", { name: "Microsoft" })).toHaveAttribute("aria-selected", "true");
  await expectNoViolations(page, "Connections, preview features off");

  // Ticket connections: Jira only.
  await page.goto("/admin/ticket-connections");
  await expect(page.getByRole("heading", { level: 1, name: "Ticket connections" })).toBeVisible();
  await expect(page.locator("[data-system]")).toHaveCount(1);
  await expect(page.locator("[data-system=jira]")).toBeVisible();
  await expect(page.locator("main")).not.toContainText(/ServiceNow|Halo/);
  await expectNoViolations(page, "Ticket connections, preview features off");

  // What each ticket system does here: Jira only.
  await expect(page.getByTestId("kept-connections")).toHaveCount(0); // nothing kept in this organization
  const sources: { id: string }[] = await (await page.request.get("/api/v1/discovery/sources")).json();
  expect(sources.map((s) => s.id)).not.toContain("okta");
  expect(sources.map((s) => s.id)).not.toContain("google_workspace");
  const systems: { system: string }[] = await (await page.request.get("/api/v1/ticket-links/systems")).json();
  expect(systems.map((s) => s.system)).toEqual(["jira"]);
  await page.context().close();
});

test("License usage isn't offered as a discovery source while its report has no screen", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const me: { capabilities: string[] } = await (await admin.request.get("/api/v1/auth/me")).json();
  expect(me.capabilities).toContain("discovery.entra"); // the other Entra sources are offered
  await admin.goto("/discovery/setup");
  await expect(admin.getByRole("link", { name: "Set up Microsoft Entra shadow IT" })).toBeVisible();
  await expect(admin.locator("[data-source=entra_licence_usage]")).toHaveCount(0);
  await expect(admin.getByRole("list", { name: "Sources" })).not.toContainText("license usage");
  // Opened by its address, it says why and offers no walkthrough.
  await admin.goto("/discovery/setup/entra_licence_usage");
  await expect(admin.getByText("Microsoft Entra license usage isn't available. It is a preview feature this installation hasn't turned on.")).toBeVisible();
  await expect(admin.getByRole("button", { name: "Continue to Connect" })).toHaveCount(0);
  await expectNoViolations(admin, "License usage setup, preview features off");
  await admin.context().close();
});
