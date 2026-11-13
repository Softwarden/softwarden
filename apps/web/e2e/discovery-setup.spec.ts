import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Connections, the guided walkthrough (Prepare, Connect, Scope, Check access, Finish with live run
 *  progress), connector settings and run history, with axe in both themes.
 *
 *  - the Entra walkthrough on the simulated tenant end to end: a connection test that fails (wrong secret) and then
 *    passes, a scope error from the API, a reload that resumes at the step in the URL, a passing access check, and a
 *    run whose counts and skipped breakdown show when it finishes;
 *  - an access check that names the missing permission (the simulator grants none: e2e/spec-org `simulate`);
 *  - a connection changed after its test reads "Changed since last test" until tested again, and runs are refused;
 *  - run history filters in the URL and a failed run's page;
 *  - every source offered to a community organization, and a source turned off for it shown as unavailable;
 *  - the read-only state for Viewers and Editors.
 *
 *  Nothing contacts Microsoft. Connectors and connections are organization-wide, so the spec runs in its own
 *  organization (`e2e/spec-org`, `discovery`), its tests in order. */

const ADMIN = "ds-admin@example.test";
const EDITOR = "ds-editor@example.test";
const VIEWER = "ds-viewer@example.test";

test.describe.configure({ mode: "serial", timeout: 240_000 });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "discovery" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const t = token();
const CONNECTION = `${t} Entra app`;
const CONNECTOR = `${t} Entra`;
const BROKEN = `${t} Entra, no consent`;
let connectorId = "";
let brokenId = "";

const heading = (page: Page, name: string) => expect(page.getByRole("heading", { name, exact: true })).toBeVisible();

test("The Entra walkthrough, from Prepare to a finished run with its counts", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/discovery");
  await page.getByRole("link", { name: "Set up a discovery source" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Set up a discovery source" })).toBeVisible();
  await page.getByRole("link", { name: "Set up Microsoft Entra ID" }).click();

  // Prepare: what to do in the Entra admin center, and the permissions reference section for this source.
  await heading(page, "Prepare");
  await expect(page.getByRole("navigation", { name: "Setup progress" }).locator("[aria-current=step]")).toContainText("Prepare");
  await expect(page.getByText(/add the Microsoft Graph application permission Application\.Read\.All/)).toBeVisible();
  await expectNoViolations(page, "Walkthrough, Prepare");
  const reference = page.getByRole("link", { name: "permissions reference, Microsoft Entra ID" });
  await expect(reference).toHaveAttribute("href", "/discovery/permissions#discovery-source-entra");
  await reference.click();
  await expect(page.getByRole("heading", { name: "Discovery source: entra", exact: true })).toBeFocused();
  await expect(page.getByTestId("permissions-doc")).toContainText("9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30");
  await expectNoViolations(page, "Permissions reference");
  await page.goBack();
  await page.getByRole("button", { name: "Continue to Connect" }).click();

  // Connect: a new connection with the wrong secret fails its test, in plain words with what to do next.
  await heading(page, "Connect");
  await page.getByRole("button", { name: "Add a Microsoft connection" }).click();
  const add = page.getByRole("dialog", { name: "Add a Microsoft connection" });
  await add.getByLabel("Name (required)").fill(CONNECTION);
  await add.getByLabel("Directory (tenant) ID (required)").fill("simulated");
  await add.getByLabel("Client secret (required)").fill("wrong-secret");
  await add.getByRole("button", { name: "Add and test" }).click();
  const result = page.getByTestId("connection-test-result");
  await expect(result).toContainText(`${CONNECTION}: Wrong credentials.`);
  await expect(result).toContainText("(code AADSTS7000215)");
  await expect(result).toContainText("Next: Create a new client secret in the app registration and paste its Value");
  await expect(page.getByRole("radio", { name: new RegExp(CONNECTION) })).toBeChecked();
  await page.getByRole("button", { name: "Save and continue to Scope" }).click();
  await expect(page.getByRole("alert").filter({ hasText: `Test ${CONNECTION} first` })).toBeVisible();
  await expectNoViolations(page, "Walkthrough, Connect with a failed test");

  // Replace the secret (write-only: set on a date, never shown), and the test passes. The dialog opens from the list's
  // copy and reads the connection again for its ETag; what is typed before that read lands is kept (it was dropped
  // and the old secret tested). The read is held here so the secret is always typed first.
  // Every read is held until the secret is typed (the dev server's StrictMode reads twice), none after.
  const reads: (() => void)[] = [];
  let holding = true;
  const held = (url: URL) => /^\/api\/v1\/connections\/microsoft\/[0-9a-f-]+$/.test(url.pathname);
  await page.route(held, async (route) => {
    if (route.request().method() === "GET" && holding) await new Promise<void>((release) => reads.push(release));
    await route.continue();
  });
  await page.getByRole("button", { name: `Edit ${CONNECTION}` }).click();
  const edit = page.getByRole("dialog", { name: `Edit ${CONNECTION}` });
  await expect(edit.getByText(/Client secret set on \d{4}-\d{2}-\d{2}/)).toBeVisible();
  await expect(edit.getByLabel("Replace client secret")).toHaveValue("");
  await edit.getByLabel("Replace client secret").fill("synthetic-secret");
  await expect.poll(() => reads.length).toBeGreaterThan(0);
  holding = false;
  reads.forEach((release) => release());
  await expect(edit.getByRole("button", { name: "Save connection" })).toBeEnabled();
  await expect(edit.getByLabel("Replace client secret")).toHaveValue("synthetic-secret");
  await edit.getByRole("button", { name: "Save connection" }).click();
  await expect(result).toContainText(`${CONNECTION}: connection works.`);
  await expect(page.getByRole("button", { name: `Edit ${CONNECTION}` })).toBeFocused();

  // A field typed before that read lands is not kept when the read shows someone else changed it meanwhile: their
  // value is shown and the form says so, instead of the typed one being saved over theirs under the new ETag. The
  // read is held and answered with other notes and a new ETag.
  const theirs: (() => void)[] = [];
  let holdingTheirs = true;
  await page.route(held, async (route) => {
    if (route.request().method() !== "GET" || !holdingTheirs) return route.fallback();
    await new Promise<void>((release) => theirs.push(release));
    const response = await route.fetch();
    const body = await response.json() as Record<string, unknown>;
    await route.fulfill({ response, json: { ...body, notes: "Changed by another Admin" }, headers: { ...response.headers(), etag: '"e2e-changed"' } });
  });
  await page.getByRole("button", { name: `Edit ${CONNECTION}` }).click();
  await edit.getByLabel("Name (required)").fill(`${CONNECTION} `); // unchanged on the server: kept
  await edit.getByLabel("Notes").fill("Typed before the read landed");
  await expect.poll(() => theirs.length).toBeGreaterThan(0);
  holdingTheirs = false;
  theirs.forEach((release) => release());
  await expect(edit.getByRole("alert").filter({ hasText: "Someone changed this connection since you opened it." })).toBeVisible();
  await expect(edit.getByLabel("Notes")).toHaveValue("Changed by another Admin");
  await expect(edit.getByLabel("Name (required)")).toHaveValue(`${CONNECTION} `);
  await edit.getByRole("button", { name: "Cancel" }).click();
  await expect(edit).toHaveCount(0);
  await page.unroute(held);
  await page.getByLabel("Connector name (required)").fill(CONNECTOR);
  await page.getByRole("button", { name: "Save and continue to Scope" }).click();

  // Scope: a value the API refuses shows on its field.
  await heading(page, "Scope");
  await expect(page).toHaveURL(/step=scope&connector=[0-9a-f-]+/);
  connectorId = new URL(page.url()).searchParams.get("connector")!;
  const tenants = page.getByLabel("Tenants whose apps count as Microsoft first-party");
  const kept = await tenants.inputValue();
  await tenants.fill(`${kept}\nnot-a-guid`);
  await page.getByRole("button", { name: "Save and continue to Check access" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Enter one tenant ID (a GUID) per line." })).toBeVisible();
  await expect(tenants).toBeFocused();
  await expectNoViolations(page, "Walkthrough, Scope with an error");
  await tenants.fill(kept);
  await page.getByLabel("Skip names starting with").fill("Graph Explorer");
  await page.getByRole("button", { name: "Save and continue to Check access" }).click();

  // A reload resumes at the step in the URL.
  await heading(page, "Check access");
  await page.reload();
  await heading(page, "Check access");
  await page.getByRole("button", { name: "Check access", exact: true }).click();
  const check = page.getByTestId("access-check");
  await expect(check).toContainText("Every required permission works.");
  await expect(check.locator("li", { hasText: "Read app registrations" })).toContainText("Works");
  await expect(check.locator("li", { hasText: "Read agent identities" })).toContainText("Not used by this scope");
  await expectNoViolations(page, "Walkthrough, Check access passed");

  // A change of scope clears the check (it said nothing about agent identities): Check and Finish ask for a new one.
  const progressNav = page.getByRole("navigation", { name: "Setup progress" });
  await progressNav.getByRole("link", { name: /^Scope/ }).click();
  await heading(page, "Scope");
  await page.getByLabel("Also list Entra Agent ID agent identities").check();
  await page.getByRole("button", { name: "Save and continue to Check access" }).click();
  await heading(page, "Check access");
  await expect(page.getByText("Not checked yet.")).toBeVisible();
  await page.getByRole("button", { name: "Continue to Finish" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Check access first." })).toBeVisible();
  await page.goto(page.url().replace("step=check", "step=finish"));
  await expect(page.getByTestId("check-first")).toContainText("Check access first.");
  await expect(page.getByRole("button", { name: "Run now" })).toHaveCount(0);
  await expectNoViolations(page, "Walkthrough, Finish before a check");
  // Back as it was, then checked again.
  await progressNav.getByRole("link", { name: /^Scope/ }).click();
  await page.getByLabel("Also list Entra Agent ID agent identities").uncheck();
  await page.getByRole("button", { name: "Save and continue to Check access" }).click();
  await expect(page.getByText("Not checked yet.")).toBeVisible();
  await page.getByRole("button", { name: "Check access", exact: true }).click();
  await expect(check).toContainText("Every required permission works.");
  await page.getByRole("button", { name: "Continue to Finish" }).click();

  // Finish: Run now, live progress, then the counts and the skipped breakdown.
  await heading(page, "Finish");
  await page.getByRole("button", { name: "Run now" }).click();
  const progress = page.getByTestId("run-progress");
  await expect(progress).toContainText("Queued");
  await expect(progress).toContainText("This page updates on its own");
  await expect(page).toHaveURL(/step=finish&connector=[0-9a-f-]+&run=[0-9a-f-]+/);
  await expectNoViolations(page, "Walkthrough, Finish while queued");
  const ran = await post(page, "e2e/run-connectors", {});
  expect(ran).toHaveLength(1);
  const done = page.getByTestId("run-result");
  await expect(done).toBeVisible();
  await expect(progress).toContainText("Succeeded");
  await expect(done.getByRole("row", { name: /Seen in the source/ })).toContainText("15");
  await expect(done.getByRole("row", { name: /published by Microsoft/ })).toContainText("4");
  await expect(done.getByRole("row", { name: /name excluded/ })).toContainText("1");
  await expectNoViolations(page, "Walkthrough, Finish with the run's counts");
  await expect(page.getByRole("link", { name: "Review the candidates" })).toHaveAttribute("href", `/discovery?connector=${connectorId}`);
  // The finished run is in the URL: a reload shows it again.
  await page.reload();
  await expect(page.getByTestId("run-result")).toBeVisible();
  await page.getByRole("link", { name: "Review the candidates" }).click();
  await expect(page.getByTestId(`connector-${CONNECTOR}`)).toContainText(/15 seen, 8 kept/);
});

test("A failing access check names the missing permission and the fix; optional ones don't block", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  const conn = await post(page, "connections/microsoft", { name: `${t} Unconsented app`, tenant_id: "simulated", secret: "synthetic-secret" });
  await post(page, `connections/microsoft/${conn.id}/test`, {});
  const c = await post(page, "connectors", { source: "entra", name: BROKEN, microsoft_connection: conn.id });
  brokenId = c.id;
  // The synthetic tenant grants this app no permission at all.
  await post(page, "e2e/spec-org", { simulate: { connector: c.id, config: { simulated_permissions: [] } } });
  await page.goto(`/discovery/setup/entra?step=check&connector=${c.id}`);
  await heading(page, "Check access");
  await page.getByRole("button", { name: "Check access", exact: true }).click();
  const check = page.getByTestId("access-check");
  await expect(check).toContainText("Missing: Application.Read.All.");
  await expect(check).toContainText("Optional, not granted (doesn't block runs): User.ReadBasic.All.");
  const failed = check.locator("li[data-state=failed]").first();
  await expect(failed).toContainText("Failed");
  await expect(failed).toContainText("Missing permission: Application.Read.All");
  await expect(failed).toContainText("Fix:");
  await expect(check.locator("li[data-state=optional_missing]")).toContainText("Optional, not granted");
  await page.getByRole("button", { name: "Continue to Finish" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Grant Application.Read.All" })).toBeVisible();
  await expectNoViolations(page, "Walkthrough, Check access failed");

  // Its run fails, and says why: a failed run for the history.
  await page.goto(`/discovery/connectors/${c.id}`);
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.getByRole("button", { name: "Run queued" })).toBeDisabled();
  await post(page, "e2e/run-connectors", {});
  await expect(page.getByRole("button", { name: "Run now" })).toBeEnabled({ timeout: 15_000 });
  await expect(page.getByRole("region", { name: "Recent runs table" })).toContainText("Failed");
});

test("A connection changed after its test says so until it is tested again; runs are refused meanwhile", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/discovery/connections");
  const row = page.getByTestId(`connection-${CONNECTION}`);
  await expect(row).toContainText("Tested");
  await expect(row).toContainText(CONNECTOR);
  await row.getByRole("button", { name: `Edit ${CONNECTION}` }).click();
  const edit = page.getByRole("dialog", { name: `Edit ${CONNECTION}` });
  await edit.getByLabel("Application (client) ID").fill("11111111-2222-4333-8444-555555555555");
  await edit.getByRole("button", { name: "Save connection" }).click();
  await expect(page.getByRole("status").filter({ hasText: "changed since its last test: test it again before discovery runs" })).toBeAttached();
  await expect(row).toContainText("Changed since last test");
  await expect(row).toContainText("Test again before discovery runs.");
  await expectNoViolations(page, "Connections, changed since last test");

  // The connector says so too, and Run now is refused with the API's reason.
  await page.goto(`/discovery/connectors/${connectorId}`);
  await expect(page.getByTestId("untested")).toContainText(`The connection ${CONNECTION} changed since its last test`);
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Not started:" })).toContainText("Test it again before running this connector.");
  await expectNoViolations(page, "Connector settings, connection not tested");

  // Tested again, it is current; a connection in use can't be deleted, and the refusal names its connectors.
  await page.goto("/discovery/connections");
  await row.getByRole("button", { name: `Test ${CONNECTION}` }).click();
  await expect(page.getByTestId("connection-test-result")).toContainText(`${CONNECTION}: connection works.`);
  await expect(row.getByTestId("test-state")).toHaveText("✓Tested");
  await row.getByRole("button", { name: `Delete ${CONNECTION}` }).click();
  const remove = page.getByRole("dialog", { name: `Delete ${CONNECTION}?` });
  await remove.getByRole("button", { name: `Delete ${CONNECTION}` }).click();
  await expect(remove.getByRole("alert")).toContainText("is used by a connector");
  await expect(remove.getByRole("link", { name: CONNECTOR })).toHaveAttribute("href", `/discovery/connectors/${connectorId}`);
  await expectNoViolations(page, "Delete a connection in use");
  await remove.getByRole("button", { name: "Cancel" }).click();
  await expect(row.getByRole("button", { name: `Delete ${CONNECTION}` })).toBeFocused();
});

test("Run history: filters in the URL, and a failed run's page with its error", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/discovery/runs");
  const table = page.getByRole("region", { name: "Run history table" });
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expectNoViolations(page, "Run history");
  await page.getByLabel("Status").selectOption("failed");
  await expect(page).toHaveURL(/status=failed/);
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table).toContainText(BROKEN);
  await page.reload();
  await expect(page.getByLabel("Status")).toHaveValue("failed");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await page.getByLabel("Status").selectOption("succeeded");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table).toContainText(CONNECTOR);
  await page.getByLabel("Status").selectOption("");
  await page.getByLabel("Connector").selectOption({ label: BROKEN });
  await expect(page).toHaveURL(new RegExp(`connector=${brokenId}`));
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await page.getByLabel("Trigger").selectOption("scheduled");
  await expect(page.getByText("No runs match these filters")).toBeVisible();
  await expectNoViolations(page, "Run history, no match");
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/discovery\/runs$/);
  await expect(table.locator("tbody tr")).toHaveCount(2);

  await table.getByRole("link", { name: new RegExp(`${BROKEN}, Failed`) }).click();
  await expect(page.getByRole("heading", { level: 1, name: new RegExp(`Run of ${BROKEN}`) })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "The run failed." })).toContainText("Application.Read.All");
  await expectNoViolations(page, "A failed run");
});

test("A run refused for an empty listing says so, and an Admin runs it once accepting the empty listing", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  const conn = await post(page, "connections/microsoft", { name: `${t} Emptied tenant`, tenant_id: "simulated", secret: "synthetic-secret" });
  await post(page, `connections/microsoft/${conn.id}/test`, {});
  const c = await post(page, "connectors", { source: "entra", name: `${t} Entra, emptied`, microsoft_connection: conn.id });
  await post(page, `connectors/${c.id}/run`, {});
  const ran: { connector: string; status: string }[] = await post(page, "e2e/run-connectors", {});
  expect(ran.find((r) => r.connector === c.id)?.status).toBe("succeeded");
  // The synthetic tenant now answers its application listings with nothing, as Graph might on revoked consent.
  await post(page, "e2e/spec-org", { simulate: { connector: c.id, config: { simulated_empty: ["/servicePrincipals", "/applications"] } } });
  await page.goto(`/discovery/connectors/${c.id}`);
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.getByRole("button", { name: "Run queued" })).toBeDisabled();
  await post(page, "e2e/run-connectors", {});
  const box = page.getByTestId("empty-listing");
  await expect(box).toContainText("The last run changed nothing: a listing came back empty.", { timeout: 15_000 });
  await expectNoViolations(page, "Connector, a run refused for an empty listing");

  const opener = box.getByRole("button", { name: "Run once, accepting an empty listing…" });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: `Run “${t} Entra, emptied” once, accepting an empty listing?` });
  await expect(dialog).toContainText("marked “no longer seen”");
  await dialog.getByRole("button", { name: "Run and accept an empty listing" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Tick the box to confirm you checked the source first.");
  await expectNoViolations(page, "Accept an empty listing dialog");
  await dialog.getByLabel("I checked in the source that these were deleted").check();
  await dialog.getByRole("button", { name: "Run and accept an empty listing" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Run queued, accepting an empty listing this once." })).toBeAttached();
  await expect(page.getByRole("heading", { level: 1 })).toBeFocused(); // the opener went with the refusal
  const accepted: { connector: string; status: string }[] = await post(page, "e2e/run-connectors", {});
  expect(accepted.find((r) => r.connector === c.id)?.status).toBe("succeeded");
  await expect(box).toHaveCount(0, { timeout: 15_000 });
  await expect(page.getByRole("region", { name: "Recent runs table" }).getByRole("row").nth(1)).toContainText("Succeeded");
  // Tidy up: the tests below count this organization's Entra connectors.
  await post(page, `connectors/${c.id}`, undefined, "DELETE");
  await post(page, `connections/microsoft/${conn.id}`, undefined, "DELETE");
  await page.context().close();
});

test("A community organization sets up every source; a source turned off for it shows as unavailable", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  const me: { edition: string; capabilities: string[] } = await (await page.request.get("/api/v1/auth/me")).json();
  expect(me.edition).toBe("community");
  for (const name of ["discovery.entra", "discovery.azure", "discovery.powerplatform", "discovery.halo"]) expect(me.capabilities).toContain(name);
  // The API takes an Azure connector from a community organization past the capability gate (a field error, not 403).
  const azure = await page.request.post("/api/v1/connectors", {
    data: { source: "azure", name: "Azure" },
    headers: { "X-CSRFToken": (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "" },
  });
  expect(azure.status()).toBe(400);
  expect(JSON.stringify(await azure.json())).not.toContain("capability-required");

  await page.goto("/discovery/setup");
  for (const label of ["Azure", "Power Platform", "HaloITSM"]) {
    const card = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: label, exact: true }) });
    await expect(card).not.toContainText("isn't available");
    await expect(page.getByRole("link", { name: `Set up ${label}` })).toBeVisible();
  }
  const entra = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: "Microsoft Entra ID", exact: true }) });
  await expect(entra).toContainText(`Already set up: ${CONNECTOR}, ${BROKEN}`);
  await expect(page.getByRole("link", { name: "Set up Microsoft Entra licenses" })).toBeVisible();
  await expectNoViolations(page, "Choose a source, community edition");
  await page.getByRole("link", { name: "Set up HaloITSM" }).click();
  await expect(page.getByText("create an API application that signs in with a client ID and secret")).toBeVisible();
  await page.goto("/discovery/connections?tab=halo");
  await expect(page.getByRole("button", { name: "Add a Halo connection" })).toBeVisible();

  // Halo discovery turned off for this organization (as platform staff would): unavailable, saying why in words,
  // never the capability name, and offering no setup. Turned back on afterwards.
  await post(page, "e2e/capability-override", { capability: "discovery.halo", revoked: true });
  try {
    await page.goto("/discovery/setup");
    const halo = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: "HaloITSM", exact: true }) });
    await expect(halo).toContainText("HaloITSM isn't available. It is part of every edition, and has been turned off for this organization");
    await expect(halo).not.toContainText("discovery.halo");
    await expect(halo.getByRole("link")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Set up Azure" })).toBeVisible();
    await expectNoViolations(page, "Choose a source, one turned off");
    await page.goto("/discovery/setup/halo");
    await expect(page.getByText("HaloITSM isn't available.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Continue to Connect" })).toHaveCount(0);
    await page.goto("/discovery/connections?tab=halo");
    await expect(page.getByText("Halo connections aren't available.")).toBeVisible();
    await expect(page.locator("[data-unavailable]")).toContainText("HaloITSM discovery is part of every edition, and has been turned off for this organization");
    await expect(page.getByRole("button", { name: "Add a Halo connection" })).toHaveCount(0);
  } finally {
    await post(page, "e2e/capability-override", { capability: "discovery.halo", revoked: false });
  }
  await page.context().close();
});

test("Viewers and Editors read a connector without its controls; set-up pages are for Admins", async ({ browser, baseURL }) => {
  for (const email of [VIEWER, EDITOR]) {
    const page = await as(browser, baseURL, email);
    await page.goto("/discovery");
    await expect(page.getByRole("button", { name: /^Run now/ })).toHaveCount(0);
    await page.getByRole("link", { name: `Details for ${CONNECTOR}`, exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: CONNECTOR })).toBeVisible();
    await expect(page.getByText("Only Admins change a connector's settings, check its access and run it.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Run now" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Save settings" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Check access again" })).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Permissions table" })).toContainText("Application.Read.All");
    await expect(page.getByTestId("access-check")).toContainText("Every required permission works.");
    if (email === VIEWER) await expectNoViolations(page, "Connector, read only");
    await page.goto("/discovery/setup/entra");
    await expect(page.getByTestId("admin-only")).toContainText("Only Admins can open Set up a discovery source.");
    await page.goto("/discovery/connections");
    await expect(page.getByTestId("admin-only")).toContainText("Only Admins can open Connections.");
    await page.goto("/discovery/runs");
    await expect(page.getByRole("region", { name: "Run history table" }).locator("tbody tr")).toHaveCount(2);
    await page.context().close();
  }
});
