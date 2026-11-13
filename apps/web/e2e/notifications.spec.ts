import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Notification channels (email settings with write-only secrets, a test email shown inline; Teams
 *  channels and webhooks with test sends), notification rules (the built-ins marked, add with lead times, channels and
 *  audience, preview, test send), the notification log (filters in the URL, due dates, baselines without Resend, resend
 *  with a confirmation and a 409 shown, a delivery's error), reminder rows in Needs attention (acknowledge, reopen,
 *  snooze, resolve; read-only for Viewers) and AdminOnly for other roles, with axe in both themes.
 *
 *  Nothing leaves the machine: email goes through the Graph simulator (tenant `simulated`) or the e2e server's
 *  in-memory outbox, and Teams and webhook posts to `*.e2e.test` are answered by the e2e server (api/e2e_views.py;
 *  `fail.` hosts answer 500). Rules and destinations are organization-wide, so the spec runs in its own organization
 *  (`e2e/spec-org`, `notify`), its tests in order. */

const ADMIN = "nt-admin@example.test";
const EDITOR = "nt-editor@example.test";
const VIEWER = "nt-viewer@example.test";

test.describe.configure({ mode: "serial" });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "notify" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

/** YYYY-MM-DD `days` from today (UTC, the spec organisation's timezone). */
const day = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
const said = (page: Page, text: string) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeAttached();
/** A contract whose cancellation deadline is `deadline` days away (end date 30 days later, 30 days' notice). */
const contract = (page: Page, name: string, deadline: number) => post(page, "contracts", {
  name, amount: "24000.00", billing_frequency: "annual", start_date: day(-300), end_date: day(deadline + 30), auto_renew: "yes", notice_value: 30, notice_unit: "days",
});

const t = token();
const TEAMS = `${t} IT alerts`;
const BROKEN = `${t} Broken channel`;
const RULE = `${t} Contracts to IT`;
const TEAMS_URL = "https://teams.e2e.test/workflows/abc/invoke?sig=TEAMS-SIGNATURE-0123456789";
const HOOK_SECRET = "hook-signing-secret-0123456789";

test("Channels: a test email inline, Graph with a write-only secret, Teams channels and webhooks with test sends", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/admin/notification-channels");
  await expect(page.getByRole("heading", { level: 1, name: "Notification channels" })).toBeVisible();
  const status = page.getByRole("region", { name: "Status" });
  await expect(status).toContainText("Installation default");
  await expect(status).toContainText("Never tested");

  // The installation default (the e2e server's in-memory outbox) sends; the result is shown inline, linked to the log.
  await page.getByRole("button", { name: "Send test email" }).click();
  const emailResult = page.getByTestId("email-test-result");
  await expect(emailResult).toContainText(`Email to ${ADMIN}: sent.`);
  await expect(emailResult.getByRole("link", { name: /View in the notification log/ })).toHaveAttribute("href", /\/admin\/notification-log\/[0-9a-f-]+$/);
  await expectNoViolations(page, "Email settings with a test result");

  // Graph with a secret the simulator refuses: saved write-only, the test fails inline with the reason.
  await page.getByRole("radio", { name: "Microsoft Graph (sendMail)" }).check();
  await page.getByLabel("Tenant ID").fill("simulated");
  await page.getByLabel("Application (client) ID").fill("11111111-2222-4333-8444-555555555555");
  await expect(page.getByText("No client secret")).toBeVisible(); // a named gap, never "Missing"
  await page.getByLabel("Client secret").fill("wrong-secret");
  await page.getByLabel("Sender mailbox").fill("softwarden@litware.example.test");
  await page.getByRole("button", { name: "Save email settings" }).click();
  await said(page, "Email settings saved.");
  await expect(page.getByText(`Client secret set on ${day(0)}`)).toBeVisible();
  await expect(page.getByLabel("Replace client secret")).toHaveValue("");
  expect(await page.content()).not.toContain("wrong-secret");
  expect(await (await page.request.get("/api/v1/notification-settings/email")).text()).not.toContain("wrong-secret");
  await page.getByRole("button", { name: "Send test email" }).click();
  await expect(emailResult).toContainText(`Email to ${ADMIN}: not sent.`);
  await expect(status).toContainText("Last test failed");

  // Replace the secret: the next test email is accepted by the simulator. Tests wait while a save is on its way (one
  // clicked meanwhile ran on the old secret); the save is held here to show it.
  let release = () => {};
  const holding = new Promise<void>((r) => { release = r; });
  await page.route((url) => url.pathname === "/api/v1/notification-settings/email", async (route) => {
    if (route.request().method() === "PATCH") await holding; // only this save is held; later ones pass at once
    await route.continue();
  });
  await page.getByLabel("Replace client secret").fill("simulated-good-secret-0123");
  await page.getByRole("button", { name: "Save email settings" }).click();
  await expect(page.getByRole("button", { name: "Saving…" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Test connection" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send test email" })).toBeDisabled();
  await expect(page.getByText("Email settings saved.")).toHaveCount(0); // the earlier note goes, so the next is said again
  release();
  await said(page, "Email settings saved.");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(emailResult).toContainText("Connection works.");
  await page.getByRole("button", { name: "Send test email" }).click();
  await expect(emailResult).toContainText(`Email to ${ADMIN}: sent.`);
  await expectNoViolations(page, "Email settings on Graph");

  // Teams channels: add (the address is never shown again, only its host), test, edit, and a failing one.
  await page.getByRole("tab", { name: "Teams channels" }).click();
  await expect(page).toHaveURL(/tab=teams/);
  await page.getByRole("button", { name: "Add Teams channel" }).click();
  let dialog = page.getByRole("dialog", { name: "Add Teams channel" });
  await dialog.getByLabel("Name (required)").fill(TEAMS);
  await dialog.getByLabel("Address (required)").fill(TEAMS_URL);
  await expectNoViolations(page, "Add Teams channel dialog");
  await dialog.getByRole("button", { name: "Add Teams channel" }).click();
  await said(page, `Teams channel ${TEAMS} added.`);
  const table = page.getByRole("region", { name: "Teams channels table" });
  const row = table.getByRole("row", { name: new RegExp(TEAMS) });
  await expect(row).toContainText("teams.e2e.test");
  await expect(row).toContainText("Never tested");
  expect(await page.content()).not.toContain("TEAMS-SIGNATURE");
  await row.getByRole("button", { name: `Send test to ${TEAMS}` }).click();
  await expect(page.getByTestId("teams-test-result")).toContainText(`Teams channel ${TEAMS}: sent.`);
  await expect(row).toContainText("Last test passed");

  await page.getByRole("button", { name: "Add Teams channel" }).click();
  dialog = page.getByRole("dialog", { name: "Add Teams channel" });
  await dialog.getByLabel("Name (required)").fill(BROKEN);
  // Office 365 connector addresses were retired: refused on the field.
  await dialog.getByLabel("Address (required)").fill("https://contoso.webhook.office.com/webhookb2/abc");
  await dialog.getByRole("button", { name: "Add Teams channel" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Office 365 connectors were retired");
  await dialog.getByLabel("Address (required)").fill("https://fail.e2e.test/workflows/broken");
  await dialog.getByRole("button", { name: "Add Teams channel" }).click();
  await said(page, `Teams channel ${BROKEN} added.`);
  const broken = table.getByRole("row", { name: new RegExp(BROKEN) });
  await broken.getByRole("button", { name: `Send test to ${BROKEN}` }).click();
  await expect(page.getByTestId("teams-test-result")).toContainText(`Teams channel ${BROKEN}: not sent.`);
  await expect(broken).toContainText("Last test failed");

  // Edit keeps the stored address unless a new one is typed.
  await row.getByRole("button", { name: `Edit ${TEAMS}` }).click();
  dialog = page.getByRole("dialog", { name: `Edit teams channel ${TEAMS}` });
  await expect(dialog).toContainText("Address set on");
  await expect(dialog.getByLabel("Replace address")).toHaveValue("");
  await dialog.getByRole("button", { name: "Save" }).click();
  await said(page, "Nothing changed.");
  await expect(row.getByRole("button", { name: `Edit ${TEAMS}` })).toBeFocused();
  await expectNoViolations(page, "Teams channels with test results");

  // Webhooks: a signed endpoint with a write-only signing secret.
  await page.getByRole("tab", { name: "Webhooks" }).click();
  await page.getByRole("button", { name: "Add webhook" }).click();
  dialog = page.getByRole("dialog", { name: "Add webhook" });
  await dialog.getByLabel("Name (required)").fill(`${t} Ticketing`);
  await dialog.getByLabel("Address (required)").fill("https://hooks.e2e.test/softwarden");
  await dialog.getByLabel("Signing secret (required)").fill(HOOK_SECRET);
  await dialog.getByRole("button", { name: "Add webhook" }).click();
  await said(page, `Webhook ${t} Ticketing added.`);
  const hook = page.getByRole("region", { name: "Webhooks table" }).getByRole("row", { name: new RegExp(`${t} Ticketing`) });
  await expect(hook).toContainText(`Secret set on ${day(0)}`);
  await hook.getByRole("button", { name: `Send test to ${t} Ticketing` }).click();
  await expect(page.getByTestId("webhooks-test-result")).toContainText(`Webhook ${t} Ticketing: sent.`);
  expect(await page.content()).not.toContain(HOOK_SECRET);
  await expectNoViolations(page, "Webhooks");
});

let ruleId = "";

test("Rules: built-ins marked, add a rule, preview the 30-day notice, test send to email and Teams", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  await contract(editor, `${t} Litware ERP`, 30); // its 30-day notice starts today: sent by the rules run
  await contract(editor, `${t} Litware HR`, 45); // inside its 60-day stage already: baselined when the rule is added
  const page = await as(browser, baseURL, ADMIN);

  await page.goto("/admin/notification-rules");
  await expect(page.getByRole("heading", { level: 1, name: "Notification rules" })).toBeVisible();
  const rules = page.getByRole("region", { name: "Notification rules table" });
  const builtin = rules.getByRole("row", { name: /^Renewals and contracts/ });
  await expect(builtin).toContainText("Built-in");
  await expect(builtin).toContainText("90, 60, 30 and 7 days before");
  await expect(rules.getByRole("row", { name: /^Daily digest/ })).toContainText("Built-in");
  await expectNoViolations(page, "Notification rules list");

  // Add: errors come back on the fields; the rule then starts quietly (baselines) and shows its settings.
  await page.getByRole("link", { name: "Add rule" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Add notification rule" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(RULE);
  await page.getByRole("button", { name: "Add rule" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Choose at least one kind of reminder." })).toBeVisible();
  await page.getByRole("group", { name: /Reminder types/ }).getByLabel("Contract cancellation deadlines").check();
  await page.getByLabel("Lead times (days before the due date)").fill("60, 30");
  await page.getByRole("group", { name: "Teams channels" }).getByLabel(new RegExp(TEAMS)).check();
  await expectNoViolations(page, "Add notification rule form");
  await page.getByRole("button", { name: "Add rule" }).click();
  await expect(page.getByRole("heading", { level: 1, name: RULE })).toBeVisible();
  await said(page, `Rule ${RULE} added. Notices already due were recorded as baselines`);
  ruleId = page.url().split("/admin/notification-rules/")[1].split("?")[0];
  await expect(page.getByLabel("Lead times (days before the due date)")).toHaveValue("60, 30");

  // Preview: the 30-day notice goes out today, to the coordinator (no routed owner) and the Teams channel.
  await page.getByRole("tab", { name: "Preview" }).click();
  await expect(page).toHaveURL(/tab=preview/);
  const preview = page.getByRole("region", { name: "Preview table" });
  const event = preview.getByRole("row", { name: new RegExp(`${t} Litware ERP`) });
  await expect(event).toContainText(day(0));
  await expect(event).toContainText("30 days before");
  await expect(event).toContainText(day(30));
  await expect(event).toContainText(`Teams: ${TEAMS}`);
  await expect(event).toContainText(ADMIN);
  await page.getByLabel("Next", { exact: true }).selectOption("7");
  await expect(page).toHaveURL(/days=7/);
  await expectNoViolations(page, "Rule preview");

  // Test send: email to me and the rule's Teams channel, each result inline.
  await page.getByRole("tab", { name: "Test send" }).click();
  await expect(page.getByLabel("Teams channel")).toHaveValue(/.+/);
  await page.getByRole("button", { name: "Send test" }).click();
  const results = page.getByTestId("test-results");
  await expect(results).toContainText(`Email to ${ADMIN}: sent.`);
  await expect(results).toContainText(`Teams channel ${TEAMS}: sent.`);
  await expectNoViolations(page, "Rule test send results");

  // A stale edit: someone else changed the rule since this page loaded it, so the save is refused (412) and nothing is
  // lost silently; "Load the latest version" shows their change.
  await page.getByRole("tab", { name: "Settings" }).click();
  const steps = page.getByLabel("Escalation steps (days after the due date)");
  await expect(steps).toHaveValue("");
  await post(page, `notification-rules/${ruleId}`, { escalation_days: [3] }, "PATCH");
  await steps.fill("2");
  await page.getByRole("button", { name: "Save rule" }).click();
  const stale = page.getByRole("alert").filter({ hasText: "Someone changed this rule since you opened it. Nothing was saved." });
  await expect(stale).toBeVisible();
  await expectNoViolations(page, "Rule form with a stale-edit banner");
  await stale.getByRole("button", { name: "Load the latest version" }).click();
  await expect(steps).toHaveValue("3");
  await expect(stale).toHaveCount(0);

  // Edit: only the changed field is sent (with If-Match); the list words it. A change that can't widen what the rule
  // sends (the name) says nothing about baselines.
  const patch = page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/notification-rules/${ruleId}`));
  await page.getByLabel("Escalation steps (days after the due date)").fill("1, 7");
  await page.getByRole("button", { name: "Save rule" }).click();
  const sentPatch = await patch;
  expect(sentPatch.postDataJSON()).toEqual({ escalation_days: [1, 7] });
  expect(sentPatch.headers()["if-match"]).toBeTruthy();
  await said(page, "Rule saved. Notices the change makes due today or earlier are recorded as baselines");
  await page.getByLabel("Name (required)").fill(`${RULE} `); // trimmed by the form: nothing to send
  await page.getByRole("button", { name: "Save rule" }).click();
  await said(page, "Nothing changed.");
  await page.getByRole("link", { name: "Notification rules" }).first().click();
  await expect(rules.getByRole("row", { name: new RegExp(RULE) })).toContainText("Escalates 1 and 7 days after");

  // A built-in can't be deleted; the daily digest's rule has only its name, on/off and time, and no preview.
  await rules.getByRole("link", { name: "Daily digest" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Daily digest" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete rule" })).toHaveCount(0);
  await expect(page.getByLabel("Send at (UTC)")).toBeVisible();
  await expect(page.getByLabel("Lead times (days before the due date)")).toHaveCount(0);
  await page.getByRole("tab", { name: "Preview" }).click();
  await expect(page.getByText("The daily digest has no preview")).toBeVisible();
  await expectNoViolations(page, "Daily digest rule");

  // A Teams channel a rule uses can't be deleted (409): the dialog says which rule. The error belongs to that opening
  // of that channel's dialog only: not shown again for another channel, nor when the same one is opened again.
  await page.goto("/admin/notification-channels?tab=teams");
  const table = page.getByRole("region", { name: "Teams channels table" });
  await table.getByRole("row", { name: new RegExp(TEAMS) }).getByRole("button", { name: `Delete ${TEAMS}` }).click();
  let dialog = page.getByRole("dialog", { name: `Delete teams channel ${TEAMS}?` });
  await dialog.getByRole("button", { name: `Delete ${TEAMS}` }).click();
  await expect(dialog.getByRole("alert")).toContainText(`${TEAMS} can't be deleted: 1 notification rule uses it (${RULE}). Remove it from the rules first.`);
  await expectNoViolations(page, "Delete Teams channel refused");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(table.getByRole("button", { name: `Delete ${TEAMS}` })).toBeFocused();
  await table.getByRole("button", { name: `Delete ${BROKEN}` }).click();
  dialog = page.getByRole("dialog", { name: `Delete teams channel ${BROKEN}?` });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await table.getByRole("button", { name: `Delete ${TEAMS}` }).click();
  dialog = page.getByRole("dialog", { name: `Delete teams channel ${TEAMS}?` });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(table.getByRole("row", { name: new RegExp(TEAMS) })).toHaveCount(1);
});

test("Log: the rule's 30-day email and Teams card, baselines without Resend, filters in the URL, resend and a delivery's error", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  // The hourly rules run, as at the organization's send time; its deliveries are attempted at once.
  const run = await post(page, "e2e/run-digest", { rules: true });
  expect(run.ended.sent).toBeGreaterThanOrEqual(2);

  // A rule id the list doesn't know stays visible in the filter rather than reading "Any rule".
  await page.goto("/admin/notification-log?rule=00000000-0000-4000-8000-000000000000");
  await expect(page.getByLabel("Rule")).toHaveValue("00000000-0000-4000-8000-000000000000");
  await expect(page.getByLabel("Rule").locator("option:checked")).toHaveText("Unknown rule");

  await page.goto(`/admin/notification-log?rule=${ruleId}`);
  await expect(page.getByRole("heading", { level: 1, name: "Notification log" })).toBeVisible();
  await expect(page.getByLabel("Rule")).toHaveValue(ruleId);
  await expect(page.getByLabel("Rule").locator("option:checked")).toHaveText(RULE);
  const log = page.getByRole("region", { name: "Notification log table" });
  const sent = log.getByRole("row").filter({ hasText: "30 days before" });
  await expect(sent).toHaveCount(2); // one email and one Teams card, at 30 days
  await expect(sent.filter({ hasText: ADMIN })).toContainText("Sent");
  await expect(sent.filter({ hasText: TEAMS })).toContainText("Sent");
  await expect(sent.first()).toContainText(day(30)); // the due date the notice was for
  const baselines = log.getByRole("row").filter({ hasText: "60 days before" });
  await expect(baselines).toHaveCount(2);
  for (const b of await baselines.all()) {
    await expect(b).toContainText("Baseline, not sent");
    await expect(b).toContainText(day(45));
    await expect(b.getByRole("button", { name: /^Resend/ })).toHaveCount(0);
  }
  await expectNoViolations(page, "Notification log, one rule");

  // Baselines only, from the filter: the URL says so and a reload keeps it.
  await page.getByLabel("Baselines").selectOption("true");
  await expect(page).toHaveURL(/baseline=true/);
  await expect(log.getByRole("row").filter({ hasText: "30 days before" })).toHaveCount(0);
  await page.reload();
  await expect(page.getByLabel("Baselines")).toHaveValue("true");
  await expect(log.getByRole("row").filter({ hasText: "60 days before" })).toHaveCount(2);
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/admin\/notification-log$/);

  // Resend a sent Teams card, confirmed; the copy is queued for the worker.
  await page.goto(`/admin/notification-log?rule=${ruleId}&baseline=false&channel=teams`);
  const card = log.getByRole("row").filter({ hasText: "30 days before" });
  await card.getByRole("button", { name: /^Resend/ }).click();
  const dialog = page.getByRole("dialog", { name: "Resend this teams message?" });
  await expect(dialog).toContainText(`goes again to ${TEAMS}`);
  await expectNoViolations(page, "Resend confirmation");
  await dialog.getByRole("button", { name: `Resend to ${TEAMS}` }).click();
  await said(page, "Resent as a new delivery");
  // The copy is queued (it has no Resend until it has an outcome); the original keeps its own.
  await expect(card).toHaveCount(2);
  await expect(card.filter({ hasText: "Resent" })).toContainText("Queued");
  await expect(card.filter({ hasText: "Resent" }).getByRole("button", { name: /^Resend/ })).toHaveCount(0);

  // The failing channel's test: its error on the delivery's page; once the channel is deleted, a resend is refused
  // (409) and the dialog says why.
  await page.goto("/admin/notification-log?kind=test&status=failed&channel=teams");
  const failed = log.getByRole("row", { name: /Test/ }).filter({ hasText: BROKEN });
  await expect(failed).toContainText("Failed");
  await failed.getByRole("link").first().click();
  await expect(page.getByRole("heading", { name: "Why it wasn't sent" })).toBeVisible();
  await expect(page.getByTestId("delivery-error")).not.toBeEmpty();
  await expect(page.getByRole("region", { name: "Adaptive card" })).toBeVisible();
  await expectNoViolations(page, "Delivery with an error");

  const channels = await (await page.request.get("/api/v1/notification-settings/teams-destinations")).json() as { id: string; name: string }[];
  const brokenId = channels.find((c) => c.name === BROKEN)!.id;
  await post(page, `notification-settings/teams-destinations/${brokenId}`, undefined, "DELETE");
  await page.getByRole("button", { name: "Resend" }).click();
  const refused = page.getByRole("dialog", { name: "Resend this teams message?" });
  await refused.getByRole("button", { name: /^Resend to/ }).click();
  await expect(refused.getByRole("alert")).toContainText("Not resent: Its Teams destination was deleted");
  await refused.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Resend" })).toBeFocused();
});

test("Needs attention reminders: acknowledge, reopen, snooze and resolve for Editors; read only for Viewers", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  const name = `${t} Litware CRM`;
  await contract(editor, name, 20);
  await editor.goto("/?scope=all");
  const section = editor.getByRole("region", { name: /^Queue table: Reminders/ });
  const row = section.getByRole("row", { name: new RegExp(name) });
  await expect(row).toContainText("Cancellation deadline");
  await expect(row).toContainText(day(20));
  await expect(row).toContainText("in 20 days");
  await expect(row).toContainText("Open");
  await expect(editor.getByRole("navigation", { name: "Queues" }).getByRole("link", { name: /^Reminders/ })).toBeVisible();
  const menu = row.getByRole("button", { name: `Reminder for ${name}, Cancellation deadline` });

  await menu.click();
  await editor.getByRole("menuitem", { name: "Acknowledge" }).click();
  await said(editor, `${name}, Cancellation deadline acknowledged`);
  await expect(row).toContainText("Acknowledged");
  await menu.click();
  await editor.getByRole("menuitem", { name: "Reopen" }).click();
  await said(editor, "reopened");
  await expect(row).toContainText("Open");

  await menu.click();
  await editor.getByRole("menuitem", { name: "Snooze…" }).click();
  let dialog = editor.getByRole("dialog", { name: `Snooze the reminder for ${name}, Cancellation deadline?` });
  await dialog.getByLabel("Snooze until").fill(day(5));
  await expectNoViolations(editor, "Snooze reminder dialog");
  await dialog.getByRole("button", { name: "Snooze reminder" }).click();
  await said(editor, `snoozed until ${day(5)}`);
  await expect(row).toContainText(`Snoozed until ${day(5)}`);
  await expect(menu).toBeFocused();
  await expectNoViolations(editor, "Needs attention with a snoozed reminder");

  // Viewers see the row and its status, with no actions.
  const viewer = await as(browser, baseURL, VIEWER);
  await viewer.goto("/?scope=all");
  const seen = viewer.getByRole("region", { name: /^Queue table: Reminders/ }).getByRole("row", { name: new RegExp(name) });
  await expect(seen).toContainText(`Snoozed until ${day(5)}`);
  await expect(seen.getByRole("button", { name: /^Reminder for/ })).toHaveCount(0);
  await expect(seen.getByRole("link", { name: new RegExp(`^View ${name}`) })).toBeVisible();

  // Resolve: a contract reminder needs a note; then the row leaves the queue.
  await menu.click();
  await editor.getByRole("menuitem", { name: "Resolve…" }).click();
  dialog = editor.getByRole("dialog", { name: `Resolve the reminder for ${name}, Cancellation deadline?` });
  await dialog.getByLabel("No longer applies").check();
  await expect(dialog.getByLabel("Note (required)")).toBeVisible();
  await dialog.getByRole("button", { name: "Resolve reminder" }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await dialog.getByLabel("Note (required)").fill("Replaced by the group-wide CRM contract.");
  await expectNoViolations(editor, "Resolve reminder dialog with an error");
  await dialog.getByRole("button", { name: "Resolve reminder" }).click();
  await said(editor, `${name}, Cancellation deadline resolved (No longer applies)`);
  await expect(section.getByRole("row", { name: new RegExp(name) })).toHaveCount(0);
});

test("Other roles: the admin pages explain who may open them, and the sidebar doesn't offer them", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  for (const [path, title] of [["/admin/notification-rules", "Notification rules"], ["/admin/notification-channels", "Notification channels"], ["/admin/notification-log", "Notification log"]]) {
    await editor.goto(path);
    await expect(editor.getByTestId("admin-only")).toContainText(`Only Admins can open ${title}.`);
  }
  await expect(editor.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /^Notification/ })).toHaveCount(0);
  await expectNoViolations(editor, "Notification log for an Editor");
  const admin = await as(browser, baseURL, ADMIN);
  await admin.goto("/admin/notification-log");
  const nav = admin.getByRole("navigation", { name: "Primary" });
  for (const label of ["Notification rules", "Notification channels", "Notification log"]) await expect(nav.getByRole("link", { name: new RegExp(`^${label}`) })).toBeVisible();
});
