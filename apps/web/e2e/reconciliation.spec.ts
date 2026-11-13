import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, showAllClear, token } from "./helpers";

/** The reconciliation queue, "Source differs", "No longer seen in source" and licence proposals, on the
 *  simulated tenant, with axe in both themes.
 *
 *  - the acceptance check end to end: link an app (from a ranked suggestion), rename it and a dismissed one in the simulator,
 *    run, see "changed" and "Source differs", apply the name (the record shows it, with an audit entry naming the
 *    connector), delete it in the simulator, run, see it in Needs attention's "No longer seen in source" (the badge
 *    equals the page's total), and keep it;
 *  - each candidate's kind (an AI agent from Copilot Studio), credentials with their end dates and scored suggestions;
 *  - undo with "also archive" refused while a licence pool references the record, then undo keeping it;
 *  - an object a connector's scope leaves out, with why;
 *  - licence proposals: create a pool for one unbound SKU, bind two existing pools (one syncing), and after a licences
 *    run only the bound syncing pool's seats change;
 *  - Viewers read all of it and change nothing.
 *
 *  Nothing contacts Microsoft. Simulator changes, the badge and the unbound SKUs are organization-wide, so the spec runs
 *  in its own organization (`e2e/spec-org`, `reconcile`), its tests in order. */

const ADMIN = "rc-admin@example.test";
const EDITOR = "rc-editor@example.test";
const VIEWER = "rc-viewer@example.test";

test.describe.configure({ mode: "serial", timeout: 240_000 });

const t = token();
const ENTRA = `${t} Entra`;
const LICENCES = `${t} Licenses`;
const ids = { entra: "", licences: "" };

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "reconcile" });
    await contoso.context().close();
    // One Microsoft connection on the simulated tenant, tested, behind an Entra connector and an Entra licences one.
    const admin = await pageAs(browser, baseURL, ADMIN);
    const conn = await post(admin, "connections/microsoft", { name: `${t} Tenant`, tenant_id: "simulated", secret: "synthetic-secret" });
    await post(admin, `connections/microsoft/${conn.id}/test`, {});
    ids.entra = (await post(admin, "connectors", { source: "entra", name: ENTRA, microsoft_connection: conn.id })).id;
    ids.licences = (await post(admin, "connectors", { source: "entra_licenses", name: LICENCES, microsoft_connection: conn.id })).id;
    await run(admin, ids.entra);
    await admin.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

/** Queue a connector's run and run it now, as the worker would within a minute. */
async function run(admin: Page, connector: string) {
  await post(admin, `connectors/${connector}/run`, {});
  const ran: { connector: string; status: string }[] = await post(admin, "e2e/run-connectors", {});
  expect(ran.find((r) => r.connector === connector)?.status).toBe("succeeded");
}

/** The simulated tenant's edits, in order, on the Entra connector only (replacing any set before). */
async function simulate(admin: Page, changes: { op: string; id: string; value?: string }[]) {
  await post(admin, "e2e/spec-org", { simulate: { connector: ids.entra, config: { simulator_changes: changes } } });
}

async function observation(page: Page, name: string): Promise<{ id: string; status: string }> {
  const body = await (await page.request.get(`/api/v1/discovery/candidates?status=all&q=${encodeURIComponent(name)}`)).json();
  return body.results.find((o: { name: string }) => o.name === name);
}

const row = (page: Page, name: string) => page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });
const filter = (page: Page, label: RegExp) => page.getByRole("button", { name: label });
const said = (page: Page, text: string | RegExp) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeVisible();

test("Rename gives changed and Source differs, applied as an audited edit; delete gives No longer seen, kept", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const viewer = await pageAs(browser, baseURL, VIEWER);
  // Records the source's apps can be: "Zoom" as the source names it, "Expense Hub" for a ranked suggestion.
  const zoom = await post(editor, "applications", { name: "Zoom", lifecycle: "active", description: "Zoom single sign-on and provisioning." });
  await post(editor, "applications", { name: "Expense Hub", lifecycle: "active" });

  await editor.goto("/discovery");
  await expect(filter(editor, /^To decide \d+/)).toHaveAttribute("aria-pressed", "true");
  // What each candidate is: an AI agent from Copilot Studio; credentials with their end dates; scored suggestions.
  await expect(row(editor, "HR Helpdesk Agent")).toContainText("AI agent: Copilot Studio");
  await expect(row(editor, "Finance Ledger SSO")).toContainText(/Client secret “Ledger API secret”: ends \d{4}-\d{2}-\d{2} \(in (19|20|21) days\)/);
  await expect(row(editor, "Graph Explorer Test")).toContainText(/Client secret “dev secret”: ! expired/);
  await expect(row(editor, "Expense Hub")).toContainText("Expense Hub, 100% name match");
  await expectNoViolations(editor, "Reconciliation queue");

  // Link from the suggestion, then link Zoom by search.
  await row(editor, "Expense Hub").getByRole("button", { name: /^Link to record[: ]+Expense Hub$/ }).click();
  const linkHub = editor.getByRole("dialog", { name: "Link to an existing record" });
  await linkHub.getByRole("button", { name: /^Expense Hub, 100%/ }).click();
  await expect(linkHub.getByRole("button", { name: /^Expense Hub, 100%/ })).toHaveAttribute("aria-pressed", "true");
  await expectNoViolations(editor, "Link dialog with suggestions");
  await linkHub.getByRole("button", { name: "Link", exact: true }).click();
  await said(editor, "Linked Expense Hub to Expense Hub.");
  await row(editor, "Zoom").getByRole("button", { name: /^Link to record[: ]+Zoom$/ }).click();
  const link = editor.getByRole("dialog", { name: "Link to an existing record" });
  await link.getByRole("combobox").fill("Zoom");
  await editor.getByRole("option", { name: /^Zoom/ }).click();
  await link.getByRole("button", { name: "Link", exact: true }).click();
  await said(editor, "Linked Zoom to Zoom.");
  // Dismiss one, for the "changed since dismissed" half.
  await row(editor, "Contract Vault").getByRole("button", { name: /^Dismiss/ }).click();
  await editor.getByLabel("Why dismiss it? (required)").fill("Legal's own repository, catalogued elsewhere");
  await editor.getByRole("dialog").getByRole("button", { name: "Dismiss" }).click();
  await said(editor, "Dismissed Contract Vault.");

  // Linking already shows what the source says differently (its IDs, homepage): apply all of it so only the rename
  // differs later.
  await filter(editor, /^Source differs \d+/).click();
  await expect(editor).toHaveURL(/status=differs/);
  await row(editor, "Zoom").getByRole("button", { name: /^Review differences/ }).click();
  let drift = editor.getByRole("dialog", { name: "Source differs: Zoom" });
  await expect(drift.getByRole("table")).toBeVisible();
  await drift.getByRole("button", { name: "Apply the source's values" }).click();
  await said(editor, /^✓ Applied the .* from .* to Zoom\./);

  // Rename both in the simulator and run.
  await simulate(admin, [{ op: "rename", id: "Zoom", value: "Zoom Workplace" }, { op: "rename", id: "Contract Vault", value: "Contract Vault 2" }]);
  await run(admin, ids.entra);
  await editor.goto("/discovery?status=changed");
  await expect(filter(editor, /^Changed since dismissed 1/)).toHaveAttribute("aria-pressed", "true");
  await expect(row(editor, "Contract Vault 2")).toContainText("Changed since dismissed");
  await editor.goto("/discovery?status=differs");
  const zoomRow = row(editor, "Zoom Workplace");
  await expect(zoomRow).toContainText("Source differs");
  await expect(zoomRow).toContainText("Name");

  // The viewer sees the differences, read-only.
  await viewer.goto("/discovery?status=differs");
  await expect(viewer.getByRole("button", { name: /^Link / })).toHaveCount(0);
  await row(viewer, "Zoom Workplace").getByRole("button", { name: /^View differences/ }).click();
  const viewDrift = viewer.getByRole("dialog", { name: "Source differs: Zoom" });
  await expect(viewDrift.getByRole("row", { name: /^Name/ })).toContainText("Zoom Workplace");
  await expect(viewDrift.getByRole("checkbox")).toHaveCount(0);
  await expectNoViolations(viewer, "Source differs, read-only");
  await viewDrift.locator("form").getByRole("button", { name: "Close" }).click();
  await expect(row(viewer, "Zoom Workplace").getByRole("button", { name: /^View differences/ })).toBeFocused();

  // Apply the name: the person reviewed "Zoom Workplace", sent as `seen` under the record's If-Match.
  await zoomRow.getByRole("button", { name: /^Review differences/ }).click();
  drift = editor.getByRole("dialog", { name: "Source differs: Zoom" });
  const name = drift.getByRole("row", { name: /^Name/ });
  await expect(name).toContainText("Zoom");
  await expect(name).toContainText("Zoom Workplace");
  await expect(name).toContainText("Differs");
  await expect(drift.getByRole("checkbox", { name: "Name" })).toBeChecked();
  await expectNoViolations(editor, "Source differs dialog");
  await drift.getByRole("button", { name: "Apply the source's values" }).click();
  await said(editor, "Applied the name from");
  await expect(zoomRow.getByRole("button", { name: /^Review differences/ })).toHaveCount(0);

  // The record has the new name, with an audit entry naming the connector.
  await editor.goto(`/applications/${zoom.id}`);
  await expect(editor.getByRole("heading", { level: 1, name: "Zoom Workplace" })).toBeVisible();
  await expect(editor.getByRole("region", { name: "Discovery sources" })).not.toContainText("Source differs");
  await editor.getByRole("tab", { name: "Audit activity" }).click();
  await expect(editor.getByRole("tabpanel")).toContainText("Zoom Workplace");
  await expect(editor.getByRole("tabpanel")).toContainText(`(from ${ENTRA})`);

  // Delete it in the simulator and run: "No longer seen in source" in Needs attention and the queue.
  await simulate(admin, [{ op: "rename", id: "Zoom", value: "Zoom Workplace" }, { op: "remove", id: "Zoom" }]);
  await run(admin, ids.entra);
  // The queue's filter agrees (the digest links here).
  await editor.goto(`/discovery?status=missing&connector=${ids.entra}`);
  await expect(row(editor, "Zoom Workplace")).toContainText("No longer seen in source");

  // Needs attention, routed to the record's IT steward (the Editor who added it).
  await editor.goto("/");
  const queue = editor.getByRole("region", { name: /^No longer seen in source/ });
  const missing = queue.getByRole("row", { name: /^Zoom Workplace/ });
  await expect(missing).toContainText(`“Zoom Workplace” in ${ENTRA}`);
  await expect(missing.getByText(/^\d{4}-\d{2}-\d{2}$/)).toBeVisible();
  // The badge counts what the page shows, this queue included.
  const nav = await editor.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /^Needs attention/ }).textContent();
  const badge = Number(/, (\d+) items/.exec(nav ?? "")![1]);
  await expect(editor.getByText(`${badge} ${badge === 1 ? "item" : "items"} derived from your catalog records.`)).toBeVisible();
  await expect(editor.getByRole("button", { name: /^Assigned to me/ })).toContainText(String(badge));
  await expect(editor.getByRole("navigation", { name: "Queues" }).getByRole("link", { name: /^No longer seen in source 1/ })).toBeVisible();
  await expectNoViolations(editor, "Needs attention with No longer seen in source");

  // The viewer sees the row and can only open the record.
  await viewer.goto("/?scope=all");
  const viewerRow = viewer.getByRole("region", { name: /^No longer seen in source/ }).getByRole("row", { name: /^Zoom Workplace/ });
  await expect(viewerRow.getByRole("link", { name: /^View record/ })).toBeVisible();
  await expect(viewerRow.getByRole("button", { name: /^Keep record/ })).toHaveCount(0);

  // Keep it, with a note.
  await missing.getByRole("button", { name: /^Keep record/ }).click();
  const keep = editor.getByRole("dialog", { name: "Keep Zoom Workplace" });
  await keep.getByLabel("Note (optional)").fill("Moved to another tenant; still in use");
  await expectNoViolations(editor, "Keep record dialog");
  await keep.getByRole("button", { name: "Keep the record" }).click();
  await said(editor, "Kept Zoom Workplace.");
  await expect(queue.getByRole("row", { name: /^Zoom Workplace/ })).toHaveCount(0);
  await showAllClear(editor); // the queue is now on the Nothing waiting line
  await expect(queue).toContainText("No records assigned to you are missing from their source.");
  await editor.goto("/discovery?status=linked&q=Zoom");
  await expect(row(editor, "Zoom Workplace")).toContainText("record kept by");
});

test("Undo with archive is refused while a license pool references the record; undo keeping it works", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  const sn = await observation(editor, "ServiceNow");
  const created = await post(editor, `discovery/observations/${sn.id}/create`, { name: `${t} ServiceNow` });
  const record = created.decision.record;
  await post(editor, "license-pools", { application: record.id, sku_name: `${t} ITSM seats`, seats_purchased: 10, seats_assigned: 4 });

  await editor.goto("/discovery?status=linked");
  await row(editor, "ServiceNow").getByRole("button", { name: /^Undo decision/ }).click();
  const undo = editor.getByRole("dialog", { name: "Undo the decision on ServiceNow" });
  await expect(undo).toContainText(`${t} ServiceNow, created from it, stays unless you also archive it.`);
  await undo.getByRole("checkbox", { name: `Also archive ${t} ServiceNow` }).check();
  await undo.getByRole("button", { name: `Undo and archive ${t} ServiceNow` }).click();
  const refusal = undo.getByRole("alert");
  await expect(refusal).toContainText("isn't archived because it has");
  await expect(refusal.getByRole("listitem")).toHaveText("1 license pool");
  await expectNoViolations(editor, "Undo refused with references");
  await undo.getByRole("checkbox", { name: `Also archive ${t} ServiceNow` }).uncheck();
  await undo.getByRole("button", { name: "Undo decision" }).click();
  await said(editor, `Undone. ServiceNow is a candidate again; ${t} ServiceNow stays as it is.`);
  await editor.goto("/discovery?status=new&q=ServiceNow");
  await expect(row(editor, "ServiceNow")).toContainText("New");
  const kept = await (await editor.request.get(`/api/v1/applications/${record.id}`)).json();
  expect(kept.archived).toBe(false);
});

test("Undo with archive archives a created record nothing references", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  const ledger = await observation(editor, "Finance Ledger SSO");
  const record = (await post(editor, `discovery/observations/${ledger.id}/create`, { name: `${t} Ledger` })).decision.record;
  await editor.goto("/discovery?status=linked&q=Finance");
  await row(editor, "Finance Ledger SSO").getByRole("button", { name: /^Undo decision/ }).click();
  const undo = editor.getByRole("dialog", { name: "Undo the decision on Finance Ledger SSO" });
  await undo.getByRole("checkbox", { name: `Also archive ${t} Ledger` }).check();
  await undo.getByRole("button", { name: `Undo and archive ${t} Ledger` }).click();
  await said(editor, `Undone, and ${t} Ledger archived.`);
  const after = await (await editor.request.get(`/api/v1/applications/${record.id}`)).json();
  expect(after.archived).toBe(true);
  expect((await observation(editor, "Finance Ledger SSO")).status).toBe("new");
});

test("Archive a record no longer seen in source, under the record's If-Match", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const payroll = await observation(admin, "Payroll Sync Daemon");
  const record = (await post(admin, `discovery/observations/${payroll.id}/create`, { name: `${t} Payroll export` })).decision.record;
  // Zoom stays removed (the first test); the payroll daemon goes too.
  await simulate(admin, [{ op: "rename", id: "Zoom", value: "Zoom Workplace" }, { op: "remove", id: "Zoom" }, { op: "remove", id: "Payroll Sync Daemon" }]);
  await run(admin, ids.entra);
  const etag = (await admin.request.get(`/api/v1/applications/${record.id}`)).headers()["etag"];
  expect(etag).toBeTruthy();
  await admin.goto("/?scope=all");
  const queue = admin.getByRole("region", { name: /^No longer seen in source/ });
  await queue.getByRole("row", { name: new RegExp(`^${t} Payroll export`) }).getByRole("button", { name: /^Archive/ }).click();
  const dialog = admin.getByRole("dialog", { name: `Archive ${t} Payroll export` });
  const submit = dialog.getByRole("button", { name: `Archive ${t} Payroll export` });
  await expect(submit).toBeEnabled(); // once the record's ETag has loaded
  await submit.click();
  await expect(dialog.getByRole("alert")).toContainText(`Confirm that ${t} Payroll export is to be archived.`);
  await dialog.getByRole("checkbox", { name: `Archive ${t} Payroll export` }).check();
  await expectNoViolations(admin, "Archive record dialog");
  const sent = admin.waitForRequest((r) => r.url().includes(`/discovery/observations/${payroll.id}/missing/archive`));
  await submit.click();
  expect((await sent).headers()["if-match"]).toBe(etag);
  await said(admin, `${t} Payroll export archived.`);
  await expect(queue.getByRole("row", { name: new RegExp(`^${t} Payroll export`) })).toHaveCount(0);
  const after = await (await admin.request.get(`/api/v1/applications/${record.id}`)).json();
  expect(after.archived).toBe(true);
});

test("An object the connector's scope leaves out says why, and is never no longer seen", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  await post(admin, `connectors/${ids.entra}`, { scope: { name_exclusions: ["Workday"] } }, "PATCH");
  await run(admin, ids.entra);
  await admin.goto("/discovery?status=out_of_scope");
  await expect(filter(admin, /^Out of scope 1/)).toHaveAttribute("aria-pressed", "true");
  const workday = row(admin, "Workday");
  await expect(workday).toContainText("Out of scope");
  await expect(workday.getByTestId("out-of-scope-why")).toContainText(`Left out by ${ENTRA}'s scope: name excluded`);
  await expect(workday.getByRole("button", { name: /^Link / })).toHaveCount(0);
  await expectNoViolations(admin, "Discovery, out of scope");
  await admin.goto("/discovery?status=missing");
  await expect(admin.getByText("Every linked record is still listed by its source")).toBeVisible();
});

test("License proposals: create a pool and bind two; after a licenses run only the bound syncing pool's seats change", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const viewer = await pageAs(browser, baseURL, VIEWER);
  const app = await post(editor, "applications", { name: `${t} Microsoft 365`, lifecycle: "active" });
  const e5 = await post(editor, "license-pools", { application: app.id, sku_name: `${t} E5 by hand`, seats_purchased: 30, seats_assigned: 2 });
  const dev = await post(editor, "license-pools", { application: app.id, sku_name: `${t} Developer pack`, seats_purchased: 5, seats_assigned: 5 });
  // Never bound to a SKU: no run touches it.
  const loose = await post(editor, "license-pools", { application: app.id, sku_name: `${t} Loose seats`, seats_purchased: 9, seats_assigned: 9 });
  await run(admin, ids.licences);

  await editor.goto("/license-pools?tab=review");
  const unbound = editor.getByRole("region", { name: "Unbound SKUs table" });
  for (const sku of ["SPE_E5", "SPE_E3", "FLOW_FREE", "DEVELOPERPACK_E5"]) await expect(unbound.getByRole("row", { name: new RegExp(`^${sku}`) })).toContainText("Proposal");
  await expect(unbound.getByRole("row", { name: /^SPE_E3/ })).toContainText("97 of 100 assigned");
  await expectNoViolations(editor, "Seat review with unbound SKUs");

  // Create a pool for SPE_E3: the application is required.
  await unbound.getByRole("row", { name: /^SPE_E3/ }).getByRole("button", { name: /^Create a pool…/ }).click();
  const create = editor.getByRole("dialog", { name: "Create a pool for SPE_E3" });
  await create.getByRole("button", { name: "Create pool" }).click();
  await expect(create.getByRole("alert")).toContainText("Choose the application these seats are for.");
  await expectNoViolations(editor, "Create a pool, with an error");
  await create.getByRole("combobox", { name: "Application (required)" }).fill(`${t} Microsoft 365`);
  await editor.getByRole("option", { name: new RegExp(`${t} Microsoft 365`) }).click();
  await create.getByRole("button", { name: "Create pool" }).click();
  await said(editor, `Added the pool SPE_E3 for ${t} Microsoft 365, with 97 of 100 assigned from ${LICENCES}.`);
  await expect(unbound.getByRole("row", { name: /^SPE_E3/ })).toHaveCount(0);

  // Bind SPE_E5 to the hand-entered pool with seat sync on, and the developer pack without it.
  await unbound.getByRole("row", { name: /^SPE_E5/ }).getByRole("button", { name: /^Bind to an existing pool…/ }).click();
  let bind = editor.getByRole("dialog", { name: "Bind SPE_E5 to an existing pool" });
  // Only pools without a SKU ID (the server's sku_id=none): the three above and the ServiceNow seats, not SPE_E3's.
  await expect(bind.getByLabel("Pool (required)").locator("option")).toHaveCount(5);
  await expect(bind.getByLabel("Pool (required)")).not.toContainText("SPE_E3");
  await bind.getByLabel("Pool (required)").selectOption({ label: `${t} E5 by hand (${t} Microsoft 365, 2 of 30 assigned)` });
  const syncBox = bind.getByRole("checkbox", { name: `Sync seat counts from ${LICENCES}` });
  await expect(syncBox).not.toBeChecked(); // as the pool has it
  await syncBox.check();
  await expectNoViolations(editor, "Bind to an existing pool");
  await bind.getByRole("button", { name: "Bind pool" }).click();
  await said(editor, `Bound ${t} E5 by hand to SPE_E5.`);
  await unbound.getByRole("row", { name: /^DEVELOPERPACK_E5/ }).getByRole("button", { name: /^Bind to an existing pool…/ }).click();
  bind = editor.getByRole("dialog", { name: "Bind DEVELOPERPACK_E5 to an existing pool" });
  await bind.getByLabel("Pool (required)").selectOption({ label: `${t} Developer pack (${t} Microsoft 365, 5 of 5 assigned)` });
  await bind.getByRole("button", { name: "Bind pool" }).click();
  await said(editor, `Bound ${t} Developer pack to DEVELOPERPACK_E5.`);

  // A licences run: only the bound syncing pool takes the source's counts.
  await run(admin, ids.licences);
  const pool = async (id: string) => (await editor.request.get(`/api/v1/license-pools/${id}`)).json();
  const [synced, held] = [await pool(e5.id), await pool(dev.id)];
  expect([synced.seats_purchased, synced.seats_assigned, synced.last_synced !== null]).toEqual([250, 231, true]);
  expect([held.seats_purchased, held.seats_assigned, held.last_synced]).toEqual([5, 5, null]);
  const untouched = await pool(loose.id);
  expect([untouched.sku_id, untouched.seats_purchased, untouched.seats_assigned, untouched.last_synced]).toEqual(["", 9, 9, null]);
  expect([synced.synced_from?.name, synced.sync_held, held.synced_from]).toEqual([LICENCES, false, null]);
  await editor.goto(`/license-pools/${e5.id}`);
  await expect(editor.getByRole("region", { name: "Pool" })).toContainText(`Seats synced from ${LICENCES}`);
  await expect(editor.getByRole("region", { name: "Seats" })).toContainText("231 of 250 seats");
  await editor.goto(`/license-pools/${dev.id}`);
  await expect(editor.getByRole("region", { name: "Pool" })).toContainText("1 proposed count");
  await expect(editor.getByRole("region", { name: "Seats" })).toContainText("5 of 5 seats");
  await expectNoViolations(editor, "Bound pool with a proposal");
  await editor.goto("/license-pools?tab=review");
  await expect(editor.getByRole("region", { name: "Unbound SKUs table" }).getByRole("row", { name: /^FLOW_FREE/ })).toBeVisible();
  await expect(editor.getByRole("region", { name: "Proposed seat counts table" })).toContainText(`${t} Developer pack`);

  // The viewer reads the proposals and can't act on them.
  await viewer.goto("/license-pools?tab=review");
  await expect(viewer.getByRole("region", { name: "Unbound SKUs table" })).toContainText("FLOW_FREE");
  await expect(viewer.getByRole("button", { name: /^Create a pool/ })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Bind to an existing pool/ })).toHaveCount(0);
  await expectNoViolations(viewer, "Seat review, read-only");
});
