import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Ticket links. The Admin's ticket connections (one per system; the secret write-only, "set on" and
 *  Replace; Test with its result in words; creating turned on), an Editor linking a pasted address with the live parse
 *  preview and its problems in words, reading a status through the simulated Jira, changing and removing a link, and
 *  creating a ticket (the confirmation of what is sent, the not-linked answer); a renewal's ServiceNow link with its
 *  cycle and a status set by hand; a Viewer's read-only section. axe in both themes.
 *
 *  The e2e server runs with the installation's preview features off, as by default (launch scope): ServiceNow
 *  and Halo ticket connections aren't offered, so only Jira is connected here, and their links are manual.
 *
 *  Ticket connections are one per system for the whole organization, so the spec runs in its own (`e2e/spec-org`,
 *  `tickets`), which starts with no connection and no link on every call. The simulated Jira is
 *  https://jira.simulated.invalid (tickets/simulator.py): ITOPS-2 is In Progress, a create answers ITOPS-100. Nothing
 *  leaves the machine. */

const ADMIN = "tk-admin@example.test";
const EDITOR = "tk-editor@example.test";
const VIEWER = "tk-viewer@example.test";
const JIRA = "https://jira.simulated.invalid";

test.describe.configure({ mode: "serial", timeout: 180_000 });

const t = token();
let assessmentId = "";
let renewalId = "";
let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "tickets" });
    await contoso.context().close();
    const editor = await pageAs(browser, baseURL, EDITOR);
    const app = await post(editor, "applications", { name: `${t} Payroll`, lifecycle: "active" });
    assessmentId = (await post(editor, "assessments", { target: app.id, change_type: "change", title: `${t} Upgrade Payroll` })).id;
    renewalId = (await post(editor, "renewals", { name: `${t} Payroll subscription`, auto_renew: "no", renewal_date: "2027-06-30" })).id;
    await editor.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const tickets = (page: Page) => page.getByRole("region", { name: /^Tickets/ });
const item = (page: Page, name: string) => tickets(page).locator(`[data-ticket="${name}"]`);

test("An Admin adds a Jira connection: the token is write-only, the test says what it found, creating is turned on", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  // No connection allows creating yet, so an Admin isn't offered Create ticket.
  await page.goto(`/assessments/${assessmentId}`);
  await expect(tickets(page).getByRole("button", { name: "Add a link" })).toBeVisible();
  await expect(tickets(page).getByRole("button", { name: "Create ticket…" })).toHaveCount(0);

  await page.goto("/admin");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Ticket connections" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Ticket connections" })).toBeVisible();
  const jira = page.locator("[data-system=jira]");
  await expect(jira).toContainText("No Jira connection.");
  // ServiceNow and Halo are preview features, off on this server as by default: only Jira is offered.
  await expect(page.locator("[data-system]")).toHaveCount(1);
  await expect(page.locator("[data-system=servicenow]")).toHaveCount(0);
  await expect(page.locator("[data-system=halo]")).toHaveCount(0);
  await expectNoViolations(page, "Ticket connections, empty");

  await jira.getByRole("button", { name: "Add a Jira connection" }).click();
  const add = page.getByRole("dialog", { name: "Add a Jira connection" });
  await add.getByLabel("Jira site address (required)").fill(JIRA);
  await add.getByLabel("Account email (required)").fill("it-bot@example.test");
  await add.getByLabel("API token (required)").fill("synthetic-jira-token");
  await expect(add.getByLabel("API token (required)")).toHaveAttribute("type", "password");
  await expectNoViolations(page, "Add a Jira connection");
  await add.getByRole("button", { name: "Add and test" }).click();
  await expect(add).toHaveCount(0);
  // The Add button that opened it is gone: focus is on the section's heading (Test is busy testing).
  await expect(page.locator("#tc-jira-h")).toBeFocused();
  await expect(page.getByTestId("ticket-test-result")).toContainText("Jira: connection works.");
  await expect(jira.getByTestId("test-state")).toHaveText("✓Tested");
  await expect(jira.getByTestId("secret-state")).toContainText(/API token set on \d{4}-\d{2}-\d{2}/);
  await expect(jira).toContainText("Never shown.");
  await expect(page.locator("body")).not.toContainText("synthetic-jira-token");
  await expect(jira.getByTestId("create-state")).toHaveText("–Off");

  // Edit: the token is never shown; Replace reveals an empty box, Keep puts the state back.
  await jira.getByRole("button", { name: "Edit Jira" }).click();
  const edit = page.getByRole("dialog", { name: "Edit Jira" });
  await expect(edit.getByTestId("secret-state")).toContainText("API token set on");
  await expect(edit.getByLabel(/API token/)).toHaveCount(0);
  await edit.getByRole("button", { name: "Replace the API token" }).click();
  await expect(edit.getByLabel("New API token")).toHaveValue("");
  await edit.getByRole("button", { name: "Keep the stored API token" }).click();
  await expect(edit.getByLabel("New API token")).toHaveCount(0);
  await edit.getByLabel("Let Editors and Admins create Jira tickets from assessments and renewals").check();
  await edit.getByLabel("Project key").fill("ITOPS");
  await edit.getByLabel("Issue type").fill("Task");
  await edit.getByRole("button", { name: "Save connection" }).click();
  await expect(edit).toHaveCount(0);
  await expect(jira.getByTestId("create-state")).toHaveText("✓On");
  await expect(jira).toContainText("Project key ITOPS, issue type Task.");

  // The dialog reads the connection again for its ETag. A field typed before that read lands is kept
  // unless the read shows someone else changed it meanwhile: their value is shown and the form says so,
  // instead of the typed one being saved over theirs. The read is held and answered with other notes and a new ETag.
  const reads: (() => void)[] = [];
  let holding = true;
  const read = (url: URL) => /^\/api\/v1\/ticket-connections\/[0-9a-f-]+$/.test(url.pathname);
  await page.route(read, async (route) => {
    if (route.request().method() !== "GET" || !holding) return route.fallback();
    await new Promise<void>((release) => reads.push(release));
    const response = await route.fetch();
    const body = await response.json() as Record<string, unknown>;
    await route.fulfill({ response, json: { ...body, notes: "Changed by another Admin" }, headers: { ...response.headers(), etag: '"e2e-changed"' } });
  });
  await jira.getByRole("button", { name: "Edit Jira" }).click();
  await edit.getByLabel("Issue type").fill("Bug"); // unchanged on the server: kept
  await edit.getByLabel("Notes").fill("Typed before the read landed");
  await expect.poll(() => reads.length).toBeGreaterThan(0);
  holding = false;
  reads.forEach((release) => release());
  await expect(edit.getByRole("alert").filter({ hasText: "Someone changed this connection since you opened it." })).toBeVisible();
  await expect(edit.getByLabel("Notes")).toHaveValue("Changed by another Admin");
  await expect(edit.getByLabel("Issue type")).toHaveValue("Bug");
  await edit.getByRole("button", { name: "Cancel" }).click();
  await expect(edit).toHaveCount(0);
  await page.unroute(read);

  // Test again: the result in words, in the status region.
  await jira.getByRole("button", { name: "Test Jira" }).click();
  await expect(page.getByTestId("ticket-test-result")).toContainText("Jira: connection works.");
  await expectNoViolations(page, "Ticket connections with Jira");

  // A new address needs the token again, so its field opens without Keep, and the API's refusal on it
  // links to that field.
  await jira.getByRole("button", { name: "Edit Jira" }).click();
  const moved = page.getByRole("dialog", { name: "Edit Jira" });
  await expect(moved.getByLabel(/API token/)).toHaveCount(0);
  await moved.getByLabel("Jira site address (required)").fill("https://other-jira.simulated.invalid");
  const token = moved.getByLabel("New API token (required)");
  await expect(token).toHaveValue("");
  await expect(moved).toContainText("The address changed, so the stored API token isn't sent there");
  await expect(moved.getByRole("button", { name: "Keep the stored API token" })).toHaveCount(0);
  await moved.getByRole("button", { name: "Save connection" }).click();
  await moved.getByRole("alert").filter({ hasText: "Fix this field" }).getByRole("link").click();
  await expect(token).toBeFocused();
  await expectNoViolations(page, "Edit Jira, a new address needs the token again");
  await moved.getByRole("button", { name: "Cancel" }).click();
  await expect(moved).toHaveCount(0);
  // A refusal on the token for any other reason opens its field too (answered here as the API words one).
  const patch = (url: URL) => /^\/api\/v1\/ticket-connections\/[0-9a-f-]+$/.test(url.pathname);
  await page.route(patch, (route) => route.request().method() !== "PATCH" ? route.fallback() : route.fulfill({
    status: 400, contentType: "application/problem+json",
    json: { type: "/problems/invalid-request", title: "Invalid request", status: 400, detail: "Check the fields.", errors: { secret: ["Enter the API token again."] } },
  }));
  await jira.getByRole("button", { name: "Edit Jira" }).click();
  await moved.getByLabel("Notes").fill("Rotated by the IT bot owner (synthetic).");
  await moved.getByRole("button", { name: "Save connection" }).click();
  await moved.getByRole("alert").filter({ hasText: "Fix this field" }).getByRole("link", { name: "Enter the API token again." }).click();
  await expect(moved.getByLabel("New API token")).toBeFocused();
  await moved.getByRole("button", { name: "Cancel" }).click();
  await page.unroute(patch);

  // A Jira connection whose token is refused: the test says so. Deleting it puts the Add button back, focused.
  await jira.getByRole("button", { name: "Edit Jira" }).click();
  const wrong = page.getByRole("dialog", { name: "Edit Jira" });
  await wrong.getByRole("button", { name: "Replace the API token" }).click();
  await wrong.getByLabel("New API token").fill("wrong-secret");
  await wrong.getByRole("button", { name: "Save connection" }).click();
  await expect(wrong).toHaveCount(0);
  await jira.getByRole("button", { name: "Test Jira" }).click();
  await expect(page.getByTestId("probe-result")).toContainText("Jira:");
  await expect(jira.getByTestId("test-state")).toHaveText("!Last test failed");
  await jira.getByRole("button", { name: "Delete Jira" }).click();
  const del = page.getByRole("dialog", { name: "Delete Jira?" });
  await expect(del).toContainText("Links to Jira tickets stay");
  await del.getByRole("button", { name: "Delete Jira" }).click();
  await expect(jira).toContainText("No Jira connection.");
  // The Delete button that opened it is gone: focus is on the Add button that replaced it.
  await expect(jira.getByRole("button", { name: "Add a Jira connection" })).toBeFocused();

  // Jira again, tested and allowing creating, for the tests below.
  await jira.getByRole("button", { name: "Add a Jira connection" }).click();
  const again = page.getByRole("dialog", { name: "Add a Jira connection" });
  await again.getByLabel("Jira site address (required)").fill(JIRA);
  await again.getByLabel("Account email (required)").fill("it-bot@example.test");
  await again.getByLabel("API token (required)").fill("synthetic-jira-token");
  await again.getByRole("button", { name: "Add and test" }).click();
  await expect(again).toHaveCount(0);
  await expect(jira.getByTestId("test-state")).toHaveText("✓Tested");
  await jira.getByRole("button", { name: "Edit Jira" }).click();
  const allow = page.getByRole("dialog", { name: "Edit Jira" });
  await allow.getByLabel("Let Editors and Admins create Jira tickets from assessments and renewals").check();
  await allow.getByLabel("Project key").fill("ITOPS");
  await allow.getByLabel("Issue type").fill("Task");
  await allow.getByRole("button", { name: "Save connection" }).click();
  await expect(allow).toHaveCount(0);
  await expect(jira.getByTestId("create-state")).toHaveText("✓On");

  // Now that Jira allows creating, the Admin is offered Create ticket.
  await page.goto(`/assessments/${assessmentId}`);
  await expect(tickets(page).getByRole("button", { name: "Create ticket…" })).toBeVisible();
  await page.context().close();
});

test("An Editor links a pasted address with its preview, reads its status, changes it, creates a ticket and removes a link", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  await page.goto(`/assessments/${assessmentId}`);
  await expect(tickets(page)).toContainText("No tickets linked");

  await tickets(page).getByRole("button", { name: "Add a link" }).click();
  const add = page.getByRole("dialog", { name: `Link a ticket to ${t} Upgrade Payroll` });
  const preview = add.getByTestId("parse-preview");
  await add.getByLabel("Ticket address (required)").fill("http://jira.simulated.invalid/browse/ITOPS-2");
  await expect(preview).toContainText("Use an https:// address.");
  await add.getByLabel("Ticket address (required)").fill(`${JIRA}/browse/ITOPS-2`);
  await expect(preview).toContainText("Jira ticket ITOPS-2");
  await expect(preview.getByTestId("parse-reads")).toHaveText("Its status is read automatically through the Jira connection.");
  await add.getByLabel("Title").fill("Assess Salesforce change");
  await expectNoViolations(page, "Link a ticket");
  await add.getByRole("button", { name: "Link ticket" }).click();
  await expect(add).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "Linked Jira ticket ITOPS-2." })).toHaveCount(1);
  const two = item(page, "ITOPS-2");
  await expect(two.locator("[data-system]")).toHaveText("Jira");
  await expect(two.getByRole("link", { name: /Assess Salesforce change/ })).toHaveAttribute("href", `${JIRA}/browse/ITOPS-2`);

  // Read its status now, through the simulated Jira.
  await two.getByRole("button", { name: "Refresh ITOPS-2" }).click();
  await expect(page.getByRole("status").filter({ hasText: "ITOPS-2 read: In progress (In Progress)." })).toHaveCount(1);
  await expect(two.locator("[data-ticket-status]")).toHaveText("◐In progress");
  await expect(two.getByTestId("ticket-read")).toContainText(/Jira says “In Progress”\. Checked \d{4}-\d{2}-\d{2}\./);

  // Change it: a status set by hand.
  await two.getByRole("button", { name: "Edit ITOPS-2" }).click();
  const edit = page.getByRole("dialog", { name: "Change the link to ITOPS-2" });
  await edit.getByLabel("Status").selectOption("resolved");
  await edit.getByRole("button", { name: "Save link" }).click();
  await expect(edit).toHaveCount(0);
  await expect(two.locator("[data-ticket-status]")).toHaveText("✓Resolved");
  await expect(two.getByTestId("ticket-read")).toContainText("Status set by hand.");

  // Back to reading it from the system: unknown until the next read, which sets it again.
  await two.getByRole("button", { name: "Edit ITOPS-2" }).click();
  const back = page.getByRole("dialog", { name: "Change the link to ITOPS-2" });
  await expect(back.getByLabel("Status")).toHaveValue("resolved");
  await back.getByLabel("Status").selectOption("");
  await back.getByRole("button", { name: "Save link" }).click();
  await expect(back).toHaveCount(0);
  await expect(two.locator("[data-ticket-status]")).toHaveText("?Unknown");
  await expect(two.getByTestId("ticket-read")).toContainText("Not read yet.");
  await two.getByRole("button", { name: "Refresh ITOPS-2" }).click();
  await expect(two.locator("[data-ticket-status]")).toHaveText("◐In progress");

  // Set to Unknown by hand after a read (through the API: the form's choice for it is "Read it from the system"): it
  // says so, never "Not read yet." though it has a check time; a refresh reads it again.
  const listed: { id: string; external_key: string }[] = await (await page.request.get(`/api/v1/assessments/${assessmentId}/ticket-links`)).json();
  const itops2 = listed.find((l) => l.external_key === "ITOPS-2")!;
  await post(page, `assessments/${assessmentId}/ticket-links/${itops2.id}`, { status: "unknown" }, "PATCH");
  await page.reload();
  await expect(two.locator("[data-ticket-status]")).toHaveText("?Unknown");
  await expect(two.getByTestId("ticket-read")).toHaveText("Unknown (set by hand).");
  await two.getByRole("button", { name: "Refresh ITOPS-2" }).click();
  await expect(two.locator("[data-ticket-status]")).toHaveText("◐In progress");

  // While platform staff act as this member, Create ticket gives way to a sentence (the API refuses it).
  await page.route("**/api/v1/auth/me", async (route) => {
    const body = await (await route.fetch()).json();
    const now = Date.now();
    await route.fulfill({ json: { ...body, impersonation: {
      id: "00000000-0000-4000-8000-000000000001", by: { id: 999, name: "Platform Support", email: "support@example.test" }, reason: "Ticket 42 (synthetic)",
      started_at: new Date(now - 60_000).toISOString(), expires_at: new Date(now + 3_600_000).toISOString(),
    } } });
  });
  await page.reload();
  await expect(tickets(page).getByTestId("create-off")).toHaveText("⊘ Creating tickets is off while you act as this member.");
  await expect(tickets(page).getByRole("button", { name: "Create ticket…" })).toHaveCount(0);
  await page.unroute("**/api/v1/auth/me");
  await page.reload();

  // Create a ticket: what is sent is said first; while the request runs the button is disabled and the dialog stays.
  await tickets(page).getByRole("button", { name: "Create ticket…" }).click();
  const create = page.getByRole("dialog", { name: `Create a ticket for ${t} Upgrade Payroll` });
  await expect(create.getByLabel("Title (required)")).toHaveValue(`${t} Upgrade Payroll`);
  await expect(create.getByTestId("create-sends")).toContainText(`The title “${t} Upgrade Payroll” and a line linking back to this assessment. Nothing else.`);
  await expectNoViolations(page, "Create a ticket");
  let release: () => void = () => undefined;
  const held = new Promise<void>((done) => { release = done; });
  const createRoute = `**/api/v1/assessments/${assessmentId}/ticket-links/create-ticket`;
  await page.route(createRoute, async (route) => { await held; await route.continue(); });
  await create.getByRole("button", { name: "Create the ticket in Jira" }).click();
  const running = create.getByRole("button", { name: "Creating in Jira…" });
  await expect(running).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(create).toBeVisible();
  await page.mouse.click(5, 5); // outside the dialog
  await expect(create).toBeVisible();
  await expect(running).toBeDisabled();
  release();
  await expect(create).toHaveCount(0);
  await page.unroute(createRoute);
  await expect(page.getByRole("status").filter({ hasText: "Created Jira ticket ITOPS-100 and linked it." })).toHaveCount(1);
  await expect(item(page, "ITOPS-100").getByTestId("ticket-read")).toContainText("Created from Softwarden by");

  // A ticket created but not linkable: its key is named so it can be linked by hand.
  await page.route(`**/api/v1/assessments/${assessmentId}/ticket-links/create-ticket`, (route) => route.fulfill({
    status: 409, contentType: "application/problem+json",
    json: { type: "/problems/conflict", title: "Conflict", status: 409, detail: "The Jira ticket ITOPS-101 was created, but couldn't be linked here. Link it by hand.", problem: "not_linked", external_key: "ITOPS-101" },
  }));
  await tickets(page).getByRole("button", { name: "Create ticket…" }).click();
  const again = page.getByRole("dialog", { name: `Create a ticket for ${t} Upgrade Payroll` });
  await again.getByRole("button", { name: "Create the ticket in Jira" }).click();
  await expect(again.getByTestId("not-linked")).toContainText("The Jira ticket ITOPS-101 was created, but couldn't be linked here.");
  await expect(again.getByRole("button", { name: /Create the ticket/ })).toHaveCount(0);
  await again.getByRole("button", { name: "Done" }).click();
  await expect(again).toHaveCount(0);
  await expect(tickets(page).getByRole("button", { name: "Create ticket…" })).toBeFocused();
  await page.unroute(`**/api/v1/assessments/${assessmentId}/ticket-links/create-ticket`);

  // A link nobody set a status on, whose read fails: unknown with the error in words, never "set by hand".
  await tickets(page).getByRole("button", { name: "Add a link" }).click();
  const missing = page.getByRole("dialog", { name: `Link a ticket to ${t} Upgrade Payroll` });
  await missing.getByLabel("Ticket address (required)").fill(`${JIRA}/browse/ITOPS-404`);
  await expect(missing.getByTestId("parse-preview")).toContainText("Jira ticket ITOPS-404");
  await missing.getByRole("button", { name: "Link ticket" }).click();
  await expect(missing).toHaveCount(0);
  const gone = item(page, "ITOPS-404");
  await expect(gone.getByTestId("ticket-read")).toHaveText("Not read yet.");
  await gone.getByRole("button", { name: "Refresh ITOPS-404" }).click();
  await expect(gone.getByTestId("ticket-error")).toBeVisible();
  await expect(gone.locator("[data-ticket-status]")).toHaveText("?Unknown");
  await expect(gone.getByTestId("ticket-read")).not.toContainText("Status set by hand.");
  await expect(gone.getByTestId("ticket-read")).not.toContainText("Not read yet.");
  await gone.getByRole("button", { name: "Edit ITOPS-404" }).click();
  const unread = page.getByRole("dialog", { name: "Change the link to ITOPS-404" });
  await expect(unread.getByLabel("Status")).toHaveValue("");
  await unread.getByRole("button", { name: "Cancel" }).click();

  // Remove one: the confirmation names it.
  await item(page, "ITOPS-100").getByRole("button", { name: "Remove ITOPS-100" }).click();
  const remove = page.getByRole("dialog", { name: "Remove the link to ITOPS-100?" });
  await remove.getByRole("button", { name: "Remove ITOPS-100" }).click();
  await expect(remove).toHaveCount(0);
  await expect(item(page, "ITOPS-100")).toHaveCount(0);
  // Its row went with it: focus is on Add a link, never the page body.
  await expect(tickets(page).getByRole("button", { name: "Add a link" })).toBeFocused();
  await expect(item(page, "ITOPS-2")).toHaveCount(1);
  await expectNoViolations(page, "Assessment with ticket links");
  await page.context().close();
});

test("A renewal's ServiceNow link names its cycle and keeps a status set by hand (ServiceNow is a preview system)", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  await page.goto(`/renewals/${renewalId}`);
  await tickets(page).getByRole("button", { name: "Add a link" }).click();
  const add = page.getByRole("dialog", { name: `Link a ticket to ${t} Payroll subscription` });
  await add.getByLabel("Ticket address (required)").fill("https://contoso.service-now.com/nav_to.do?uri=incident.do?sysparm_query=number=INC0000002");
  await expect(add.getByTestId("parse-preview")).toContainText("ServiceNow ticket INC0000002");
  // Preview features are off on this server: ServiceNow statuses are never read here, so the preview says so.
  await expect(add.getByTestId("parse-reads")).toHaveText("Its status isn't read automatically: set it by hand below.");
  // Unknown isn't a choice by hand: "Read it from the system" is that.
  await expect(add.getByLabel("Status").locator("option")).toHaveText(["Read it from the system", "Open", "In progress", "Resolved", "Closed"]);
  await add.getByLabel("Status").selectOption("in_progress");
  await add.getByLabel("Cycle").selectOption({ label: "Cycle 1, renews 2027-06-30" });
  await add.getByRole("button", { name: "Link ticket" }).click();
  await expect(add).toHaveCount(0);
  const inc = item(page, "INC0000002");
  await expect(inc.locator("[data-system]")).toHaveText("ServiceNow");
  await expect(inc.locator("[data-ticket-status]")).toHaveText("◐In progress");
  await expect(inc.getByTestId("ticket-read")).toContainText("Status set by hand.");
  await expect(inc.getByTestId("ticket-read")).toContainText("About cycle 1, renews 2027-06-30.");
  // Not offered: its system isn't read here.
  await expect(inc.getByRole("button", { name: "Refresh INC0000002" })).toHaveCount(0);
  await expect(inc.getByRole("button", { name: "Edit INC0000002" })).toBeVisible();
  await expectNoViolations(page, "Renewal with a ticket link");
  await page.context().close();
});

test("A Viewer reads the tickets and can't change them", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  await page.goto(`/assessments/${assessmentId}`);
  await expect(item(page, "ITOPS-2").locator("[data-ticket-status]")).toHaveText("◐In progress");
  await expect(tickets(page).getByRole("button")).toHaveCount(0);
  await expect(tickets(page).getByTestId("tickets-read-only")).toHaveText("Editors and Admins add, change and refresh ticket links.");
  await expectNoViolations(page, "Assessment tickets (Viewer)");
  await page.goto("/admin/ticket-connections");
  await expect(page.getByTestId("admin-only")).toContainText("Only Admins can open Ticket connections.");
  await page.context().close();
});

test("Deleting a Halo connection a kept Halo ticket connection signs in through is refused, naming it, until the kept one is deleted", async ({ browser, baseURL }) => {
  // Runs in the enterprise organization (every edition has discovery.halo), put back afterwards.
  // Halo ticket connections are a preview feature, off on this server as by default, so the API can't create one: the
  // test-only route writes one as kept from when preview features were on. It still blocks the delete (data is kept).
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const { email } = await post(contoso, "e2e/enterprise-org", { which: "secondary" });
  await contoso.context().close();
  const page = await pageAs(browser, baseURL, email);
  const name = `${t} Halo`;
  // Made inside the try, so whatever was made is removed even when a later step fails.
  let halo: { id: string } | null = null;
  let ticket: { id: string } | null = null;
  try {
    halo = await post(page, "connections/halo", { name, base_url: "https://halo.simulated.invalid", client_id: "sw", scope: "read:assets read:tickets", secret: "synthetic-halo-secret" });
    await post(page, `connections/halo/${halo!.id}/test`, {});
    ticket = await post(page, "e2e/kept-ticket-connection", { name: `${t} Halo tickets`, halo_connection: halo!.id });
    await page.goto("/discovery/connections?tab=halo");
    await page.getByTestId(`connection-${name}`).getByRole("button", { name: `Delete ${name}` }).click();
    const del = page.getByRole("dialog", { name: `Delete ${name}?` });
    await del.getByRole("button", { name: `Delete ${name}` }).click();
    const users = del.getByRole("list", { name: "Ticket connections using it" });
    await expect(users.getByRole("link", { name: `Ticket connection ${t} Halo tickets` })).toHaveAttribute("href", "/admin/ticket-connections");
    await expectNoViolations(page, "Halo connection delete refused by a ticket connection");

    // The way out with preview features off: the kept ticket connection is listed and can be deleted, then the Halo
    // connection can be.
    await users.getByRole("link", { name: `Ticket connection ${t} Halo tickets` }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Ticket connections" })).toBeVisible();
    const keptList = page.getByTestId("kept-connections");
    await expect(keptList.getByRole("heading", { name: "Kept from a preview install" })).toBeVisible();
    const row = keptList.locator("[data-kept=halo]").filter({ hasText: `${t} Halo tickets` });
    await expect(row.getByRole("button")).toHaveText([`Delete ${t} Halo tickets`]); // Delete is all it offers
    await expectNoViolations(page, "Ticket connections with a kept Halo ticket connection");
    await row.getByRole("button", { name: `Delete ${t} Halo tickets` }).click();
    const delTicket = page.getByRole("dialog", { name: `Delete ${t} Halo tickets?` });
    await delTicket.getByRole("button", { name: `Delete ${t} Halo tickets` }).click();
    await expect(delTicket).toHaveCount(0);
    await expect(page.getByRole("status").filter({ hasText: `${t} Halo tickets deleted.` }).first()).toBeVisible();
    await expect(page.getByTestId("kept-connections")).toHaveCount(0);
    ticket = null;
    await page.goto("/discovery/connections?tab=halo");
    await page.getByTestId(`connection-${name}`).getByRole("button", { name: `Delete ${name}` }).click();
    const again = page.getByRole("dialog", { name: `Delete ${name}?` });
    await again.getByRole("button", { name: `Delete ${name}` }).click();
    await expect(again).toHaveCount(0);
    await expect(page.getByTestId(`connection-${name}`)).toHaveCount(0);
    halo = null;
  } finally {
    if (ticket) await post(page, "e2e/kept-ticket-connection", { delete: ticket.id }).catch(() => undefined);
    if (halo) await post(page, `connections/halo/${halo.id}`, {}, "DELETE").catch(() => undefined);
    await page.context().close();
  }
});
