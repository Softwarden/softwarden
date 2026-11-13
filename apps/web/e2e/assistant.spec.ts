import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post } from "./helpers";

/** The assistant (capability `assistant`), with axe in both themes.
 *
 *  - an Admin sets an API key and a cap (write-only: "set on DATE", never shown) and sees which key is in use;
 *  - an Editor asks the assistant, the batch's progress shows, and each candidate shows its suggestion: the action and
 *    record, the confidence as glyph, percent and words, and the assistant's reason as plain text (the fake model's
 *    reason carries markup and an address, which must stay text); a suggestion the API discarded says why;
 *  - an Editor accepts a link (the outcome reads Accepted), accepts a dismiss with its optional reason, and rejects one
 *    with a note; a Viewer reads all of it and can't decide;
 *  - the cap reached: the batch stops, the queue and the settings say so with when;
 *  - the usage log lists the calls with their tokens and costs, its month in the URL;
 *  - a suggestion made before the candidate changed reads "Out of date", one the model declined says so;
 *  - settings changed by someone else meanwhile: a 412 with "Load the latest version"; removing the stored key;
 *  - Undo after accepting a link, and the default reason of an accepted dismiss in the Dismissed view;
 *  - an organization whose assistant was turned off sees the capability gate, and its queue has no assistant column
 *    or panel;
 *  - the guided setup's Prepare step lists a source's permissions before any connector of it exists.
 *
 *  No Anthropic call is ever made: the e2e server's discovery.assistant.make_client (api/e2e_views.py, installed only
 *  on the Playwright API server) answers the assistant spec organization's own key with a fake client and refuses any
 *  other. The assistant's settings and spend are organization-wide, so the spec runs in its own
 *  organization (`e2e/assistant-org`), its tests in order. */

const ADMIN = "as-admin@example.test";
const EDITOR = "as-editor@example.test";
const VIEWER = "as-viewer@example.test";
// Marked as a test key; the fake client accepts only what this organization stored.
const KEY = "sk-ant-e2e-fake-key-0000000000";

test.describe.configure({ mode: "serial", timeout: 180_000 });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/assistant-org", { setup: true });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

/** Run the organization's queued assistant batches now, as the worker would within a minute (through the fake). */
async function runBatches(browser: Browser, baseURL: string | undefined) {
  const admin = await pageAs(browser, baseURL, ADMIN);
  await post(admin, "e2e/assistant-org", { run: true });
  await admin.context().close();
}

const row = (page: Page, name: string) => page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });
const suggestion = (page: Page, name: string) => row(page, name).getByTestId("assistant-suggestion");
const said = (page: Page, text: string | RegExp) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeVisible();

test("An organization whose assistant was turned off sees the capability gate and no assistant anywhere", async ({ browser, baseURL }) => {
  await (await as(browser, baseURL, ADMIN)).context().close();
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const community = await post(contoso, "e2e/assistant-org", { community: true });
  await contoso.context().close();
  const page = await pageAs(browser, baseURL, community.accounts.admin);
  await page.goto("/admin/assistant");
  await expect(page.getByRole("heading", { level: 1, name: "Assistant" })).toBeVisible();
  await expect(page.getByTestId("assistant-gate")).toContainText("The assistant isn't available.");
  // Turned off for this organization, never an internal capability name.
  await expect(page.getByTestId("assistant-gate")).toContainText("It is part of every edition, and has been turned off for this organization");
  await expect(page.getByTestId("assistant-gate")).not.toContainText("capability");
  await expect(page.getByRole("navigation").getByRole("link", { name: "Assistant", exact: true })).toHaveCount(0);
  await expectNoViolations(page, "Assistant, capability gate");
  await page.goto("/admin/assistant/usage");
  await expect(page.getByTestId("assistant-gate")).toBeVisible();
  await page.context().close();

  // A community organization's queue: its candidates, no assistant column, panel or suggestion.
  const editor = await pageAs(browser, baseURL, community.accounts.editor);
  await editor.goto("/discovery");
  await expect(row(editor, "Zoom")).toBeVisible();
  await expect(editor.getByRole("columnheader", { name: "Assistant's suggestion" })).toHaveCount(0);
  await expect(editor.getByTestId("assistant-panel")).toHaveCount(0);
  await expect(editor.getByRole("button", { name: "Ask the assistant" })).toHaveCount(0);
  await expectNoViolations(editor, "Discovery queue without the assistant");
  await editor.context().close();
});

test("An Admin sets the API key and the monthly cap; the key is never shown", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/discovery");
  await page.getByRole("navigation").getByRole("link", { name: "Assistant", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Assistant" })).toBeVisible();
  await expect(page.getByTestId("key-source")).toContainText("No API key");
  await expect(page.getByTestId("assistant-meter")).toContainText("0.00 USD of 10.00 USD");
  await expect(page.getByRole("link", { name: "permissions reference, AI assistant" })).toHaveAttribute("href", "/discovery/permissions#ai-assistant-anthropic-off-by-default");
  await expectNoViolations(page, "Assistant settings, not set up");

  // Turning it on without a key is refused by the API, in words, on the field.
  await page.getByRole("switch", { name: "Turn the assistant on" }).check();
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Enter an API key first" })).toBeVisible();

  await page.getByLabel("Anthropic API key").fill(KEY);
  await page.getByLabel("Monthly cap (USD)").fill("5.00");
  await page.getByRole("button", { name: "Save settings" }).click();
  await said(page, "Saved: the key was replaced, the assistant is on, the cap is 5.00 USD.");
  await expect(page.getByTestId("key-source")).toContainText("This organization's own key");
  await expect(page.getByTestId("key-source")).toContainText(/set on \d{4}-\d{2}-\d{2}; never shown/);
  await expect(page.getByLabel("Replace the API key")).toHaveValue("");
  await expect(page.getByTestId("assistant-meter")).toContainText("of 5.00 USD");
  expect(await page.content()).not.toContain(KEY);
  await expectNoViolations(page, "Assistant settings, saved");

  // Someone else saves meanwhile: this page's save is refused (412) and offers their version.
  await post(page, "assistant/settings", { monthly_cap_usd: "6.00" }, "PATCH");
  await page.getByLabel("Monthly cap (USD)").fill("7.00");
  await page.getByRole("button", { name: "Save settings" }).click();
  const stale = page.getByRole("alert").filter({ hasText: "Someone else changed the assistant's settings" });
  await expect(stale).toBeVisible();
  await expectNoViolations(page, "Assistant settings, changed meanwhile");
  await stale.getByRole("button", { name: "Load the latest version" }).click();
  await expect(page.getByLabel("Monthly cap (USD)")).toHaveValue("6.00");
  await expect(page.getByTestId("assistant-meter")).toContainText("of 6.00 USD");
  await page.context().close();
});

test("An Editor asks the assistant and sees each suggestion with its confidence and reason", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  await editor.goto("/discovery");
  const panel = editor.getByTestId("assistant-panel");
  await expect(panel).toContainText("The assistant hasn't looked at any candidates yet.");
  await expect(editor.getByRole("columnheader", { name: "Assistant's suggestion" })).toBeVisible();
  await expect(suggestion(editor, "Zoom")).toHaveCount(0);
  await expect(row(editor, "Zoom")).toContainText("No suggestion yet");
  await panel.getByRole("button", { name: "Ask the assistant" }).click();
  await expect(panel).toContainText("Queued");
  await expect(panel.getByRole("button", { name: "Assistant working" })).toBeDisabled();
  await runBatches(browser, baseURL);
  await expect(editor.getByTestId("assistant-batch")).toContainText("looked at 5 candidates; 4 suggested, 1 without a usable answer; 1 call.", { timeout: 15_000 });
  await expect(panel.getByRole("button", { name: "Ask the assistant" })).toBeEnabled();

  // Link, with the record, the confidence in glyph, percent and words, and the reason as plain text.
  const zoom = suggestion(editor, "Zoom");
  await expect(zoom).toContainText("Link to an existing record: Zoom");
  await expect(zoom.getByTestId("assistant-confidence")).toHaveText("● 93%, high confidence");
  await expect(zoom.getByTestId("assistant-text")).toHaveText("Assistant's reason: <b>Same</b> name as the record Zoom; see https://example.test/x.");
  await expect(zoom.locator("b")).toHaveCount(0);
  await expect(zoom.getByTestId("assistant-text").getByRole("link")).toHaveCount(0);
  await expect(suggestion(editor, "Expense Hub")).toContainText("Create a record");
  await expect(suggestion(editor, "Expense Hub")).toContainText("◐ 71%, medium confidence");
  await expect(suggestion(editor, "Test registration 7")).toContainText("Dismiss");
  // Discarded: the answer named a record that wasn't offered.
  await expect(suggestion(editor, "Mystery Widget")).toContainText("No usable suggestion");
  await expect(suggestion(editor, "Mystery Widget")).toContainText("Why it was discarded: The answer named a record that wasn't offered for this candidate, so it was discarded.");
  await expect(row(editor, "Mystery Widget").getByRole("button", { name: /^Accept suggestion/ })).toHaveCount(0);
  await expectNoViolations(editor, "Discovery queue with suggestions");
  await editor.context().close();
});

test("An Editor accepts a link, accepts a dismiss and rejects one; each outcome is shown", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  await editor.goto("/discovery");
  await row(editor, "Zoom").getByRole("button", { name: "Accept suggestion for Zoom" }).click();
  await said(editor, "Accepted the assistant's suggestion: linked Zoom to Zoom. Nothing on Zoom changed.");
  await expect(row(editor, "Zoom")).toHaveCount(0); // decided, so no longer to decide
  // The row is gone, so focus is on the notice's Undo. Undo makes it a candidate with its suggestion undecided again.
  const undo = editor.getByRole("button", { name: "Undo", exact: true });
  await expect(undo).toBeFocused();
  await undo.click();
  await said(editor, "Undone. Zoom is a candidate again.");
  await expect(suggestion(editor, "Zoom").getByTestId("assistant-outcome")).toHaveCount(0);
  await row(editor, "Zoom").getByRole("button", { name: "Accept suggestion for Zoom" }).click();
  await said(editor, "Accepted the assistant's suggestion: linked Zoom to Zoom.");
  await editor.getByRole("button", { name: /^Linked \d+/ }).click();
  await expect(editor).toHaveURL(/status=linked/);
  const accepted = suggestion(editor, "Zoom").getByTestId("assistant-outcome");
  await expect(accepted).toContainText("✓Accepted");
  await expect(accepted).toContainText("Avery Editor accepted it");
  await expectNoViolations(editor, "Accepted suggestion");

  await editor.goto("/discovery");
  // Dismiss, accepted with the optional reason left empty: the assistant's own reason is saved.
  await row(editor, "Test registration 7").getByRole("button", { name: "Accept suggestion… for Test registration 7" }).click();
  const dismiss = editor.getByRole("dialog", { name: "Accept: dismiss Test registration 7" });
  await expect(dismiss).toContainText("Assistant's reason: A developer's test app.");
  await expectNoViolations(editor, "Accept a dismiss suggestion");
  await dismiss.getByRole("button", { name: "Dismiss" }).click();
  await said(editor, "Accepted the assistant's suggestion: dismissed Test registration 7.");
  // The reason left empty: the decision keeps the assistant's, marked as accepted.
  await editor.getByRole("button", { name: /^Dismissed \d+/ }).click();
  await expect(row(editor, "Test registration 7")).toContainText("“Assistant suggestion accepted: A developer's test app.”");
  await expect(suggestion(editor, "Test registration 7").getByTestId("assistant-outcome")).toContainText("✓Accepted");
  await editor.goto("/discovery");

  // Reject, with a note: the candidate stays to decide, the suggestion says Rejected with the note.
  await row(editor, "Expense Hub").getByRole("button", { name: "Reject… the suggestion for Expense Hub" }).click();
  const reject = editor.getByRole("dialog", { name: "Reject the suggestion for Expense Hub" });
  await reject.getByLabel("Why not? (optional)").fill("Built in house; catalogued under another name");
  await expectNoViolations(editor, "Reject a suggestion");
  await reject.getByRole("button", { name: "Reject suggestion" }).click();
  await said(editor, "Rejected the assistant's suggestion for Expense Hub. It is still to decide.");
  const rejected = suggestion(editor, "Expense Hub");
  await expect(rejected.getByTestId("assistant-outcome")).toContainText("✕Rejected");
  await expect(rejected).toContainText("Note: “Built in house; catalogued under another name”");
  await expect(row(editor, "Expense Hub").getByRole("button", { name: /^Accept suggestion/ })).toHaveCount(0);
  // Still a candidate: it can be decided by hand.
  await expect(row(editor, "Expense Hub").getByRole("button", { name: "Create record from Expense Hub" })).toBeVisible();
  await editor.context().close();
});

test("A Viewer reads the suggestions and outcomes but can't ask or decide", async ({ browser, baseURL }) => {
  const viewer = await as(browser, baseURL, VIEWER);
  await viewer.goto("/discovery");
  const panel = viewer.getByTestId("assistant-panel");
  await expect(panel).toContainText("Finished");
  await expect(panel.getByRole("button")).toHaveCount(0);
  await expect(suggestion(viewer, "Expense Hub")).toContainText("Rejected");
  await expect(suggestion(viewer, "Mystery Widget")).toContainText("No usable suggestion");
  await expect(viewer.getByRole("button", { name: /^Accept suggestion|^Reject…/ })).toHaveCount(0);
  await expectNoViolations(viewer, "Discovery queue, Viewer with suggestions");
  await viewer.context().close();
});

test("A suggestion made before the candidate changed is out of date; one the model declined says so", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  await post(admin, "e2e/assistant-org", { change: ["Old Notes App"] });
  const editor = await pageAs(browser, baseURL, EDITOR);
  await editor.goto("/discovery");
  const old = suggestion(editor, "Old Notes App");
  await expect(old).toContainText("Create a record");
  await expect(old).toContainText("◐Out of date");
  await expect(old).toContainText("Made before the candidate last changed in the source, so it can't be accepted.");
  await expect(row(editor, "Old Notes App").getByRole("button", { name: /^Accept suggestion|^Reject…/ })).toHaveCount(0);

  // A call carrying "Declined Thing" is refused by the model: every candidate in it says so.
  await post(admin, "e2e/assistant-org", { observe: ["Declined Thing"] });
  await editor.reload();
  await editor.getByTestId("assistant-panel").getByRole("button", { name: "Ask the assistant" }).click();
  await expect(editor.getByTestId("assistant-panel")).toContainText("Queued");
  await runBatches(browser, baseURL);
  await expect(editor.getByTestId("assistant-batch")).toContainText("3 declined by the model", { timeout: 15_000 });
  const declined = suggestion(editor, "Declined Thing");
  await expect(declined).toContainText("No suggestion: declined by the model");
  await expect(declined).toContainText("Details from the assistant: The model declined to suggest anything for this candidate (refusal: e2e).");
  await expect(declined).toContainText("The assistant isn't asked again unless the candidate changes.");
  await expectNoViolations(editor, "Discovery queue, out of date and declined");
  await editor.context().close();
  await admin.context().close();
});

test("The cap reached: the batch stops before the call, and the queue and the settings say so", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  // The dev server's StrictMode mounts the page twice, so it reads the settings twice; the first read's answer, landing
  // after the cap was typed, reset the form and nothing was saved. It is held here until the
  // cap is typed, then let through: the page no longer wants it, and the typed cap stays.
  let release = () => {};
  const held = new Promise<void>((r) => { release = r; });
  let reads = 0;
  const settings = (url: URL) => url.pathname === "/api/v1/assistant/settings";
  await admin.route(settings, async (route) => {
    if (route.request().method() !== "GET" || ++reads > 1) return route.fallback();
    await held;
    await route.continue();
  });
  await admin.goto("/admin/assistant");
  await admin.getByLabel("Monthly cap (USD)").fill("0.01");
  expect(reads).toBe(2);
  const late = admin.waitForResponse((r) => settings(new URL(r.url())) && r.request().method() === "GET");
  release();
  await late;
  await admin.evaluate(() => new Promise((r) => setTimeout(r, 200))); // the late answer is handled by the page
  await expect(admin.getByLabel("Monthly cap (USD)")).toHaveValue("0.01");
  await admin.unroute(settings);
  await admin.getByRole("button", { name: "Save settings" }).click();
  await said(admin, "Saved: the cap is 0.01 USD.");
  await post(admin, "e2e/assistant-org", { observe: ["Later Tool"] });

  const editor = await pageAs(browser, baseURL, EDITOR);
  await editor.goto("/discovery");
  await editor.getByTestId("assistant-panel").getByRole("button", { name: "Ask the assistant" }).click();
  await expect(editor.getByTestId("assistant-panel")).toContainText("Queued");
  await runBatches(browser, baseURL);
  const panel = editor.getByTestId("assistant-panel");
  await expect(panel).toContainText("Stopped early", { timeout: 15_000 });
  await expect(panel).toContainText("Cap reached");
  await expect(editor.getByTestId("assistant-stop")).toContainText("Cap reached: the next call could take this month's cost past the monthly cap.");
  await expect(row(editor, "Later Tool")).toContainText("No suggestion yet");
  await expectNoViolations(editor, "Discovery queue, cap reached");
  await editor.context().close();

  await admin.reload();
  await expect(admin.getByTestId("cap-reached")).toContainText(/Cap reached\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}/);
  await expect(admin.getByTestId("assistant-meter")).toContainText("0.0084 USD of 0.01 USD (84%); 0.0016 USD left");
  await expectNoViolations(admin, "Assistant settings, cap reached");
  await admin.context().close();
});

test("The usage log lists the calls with tokens and costs, its month in the URL", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  await admin.goto("/admin/assistant");
  await admin.getByRole("link", { name: "Usage log" }).click();
  await expect(admin.getByRole("heading", { level: 1, name: "Assistant usage log" })).toBeVisible();
  const table = admin.getByRole("region", { name: "Assistant usage table" });
  // Two calls, newest first: the declined one, then the first; the cap-reached batch was stopped before it made one.
  await expect(table.getByRole("row")).toHaveCount(3);
  await expect(table.getByRole("row").nth(1)).toContainText("Declined by the model");
  const call = table.getByRole("row").nth(2);
  await expect(call).toContainText("Answered");
  await expect(call).toContainText("1,200");
  await expect(call).toContainText("180");
  await expect(call).toContainText("0.023605 USD"); // the worst case reserved
  await expect(call).toContainText("0.0042 USD"); // what it cost
  await expect(call).toContainText("claude-sonnet-5-5");
  await expect(admin.getByTestId("usage-total")).toContainText("Cap reached");
  await expect(admin.getByTestId("usage-truncated")).toHaveCount(0);
  await expectNoViolations(admin, "Assistant usage log");

  const month = await admin.getByLabel("Month", { exact: true }).inputValue();
  await admin.getByRole("button", { name: "Previous month" }).click();
  await expect(admin).toHaveURL(/month=\d{4}-\d{2}/);
  await expect(admin.getByLabel("Month", { exact: true })).not.toHaveValue(month);
  await expect(admin.getByText(/^No calls in /)).toBeVisible();
  await expectNoViolations(admin, "Assistant usage log, a month without calls");
  await admin.reload();
  await expect(admin.getByText(/^No calls in /)).toBeVisible(); // the month came back from the URL
  await admin.getByRole("button", { name: "This month" }).click();
  await expect(admin).not.toHaveURL(/month=/);
  await expect(table.getByRole("row")).toHaveCount(3);
  // A month that isn't one is said so, and never asked for.
  await admin.goto("/admin/assistant/usage?month=2026-13");
  await expect(admin.getByRole("alert")).toContainText("“2026-13” isn't a month.");
  await admin.context().close();
});

test("An Admin removes the stored key; the page says no key is in use", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  await admin.goto("/admin/assistant");
  await admin.getByRole("switch", { name: "Turn the assistant on" }).uncheck();
  await admin.getByLabel(/^Remove the stored key/).check();
  await admin.getByRole("button", { name: "Save settings" }).click();
  await said(admin, "Saved: the key was removed, the assistant is off.");
  await expect(admin.getByTestId("key-source")).toContainText("No API key");
  await expect(admin.getByLabel("Anthropic API key")).toHaveValue("");
  await expect(admin.getByLabel(/^Remove the stored key/)).toHaveCount(0);
  await expectNoViolations(admin, "Assistant settings, key removed");
  await admin.context().close();
});

test("Prepare lists a source's permissions before any connector of it exists", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const connectors: { source: string }[] = await (await admin.request.get("/api/v1/connectors")).json();
  expect(connectors.some((c) => c.source === "azure")).toBe(false);
  await admin.goto("/discovery/setup/azure");
  await expect(admin.getByRole("heading", { name: "Prepare", exact: true })).toBeVisible();
  const permissions = admin.getByRole("region", { name: "Permissions table" });
  await expect(permissions.getByRole("rowheader", { name: /^Reader/ })).toBeVisible();
  await expect(permissions).toContainText("Azure role");
  await expect(permissions).toContainText("Required");
  await expectNoViolations(admin, "Walkthrough, Prepare without a connector");
  await admin.context().close();
});

test("A run's page has its own address; the old one with the connector redirects to it", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const [connector]: { id: string; name: string }[] = await (await admin.request.get("/api/v1/connectors")).json();
  const name = connector.name.replace(/[()]/g, "\\$&");
  const queued = await post(admin, `connectors/${connector.id}/run`, {});
  const run = queued.active_run.id as string;
  await post(admin, "e2e/run-connectors", {});
  await admin.goto(`/discovery/runs/${connector.id}/${run}`);
  await expect(admin).toHaveURL(new RegExp(`/discovery/runs/${run}$`));
  await expect(admin.getByRole("heading", { level: 1, name: new RegExp(`^Run of ${name}`) })).toBeVisible();
  await expect(admin.getByTestId("run-result")).toBeVisible();
  await admin.goto("/discovery/runs");
  await admin.getByRole("link", { name: new RegExp(`, ${name}, Succeeded$`) }).first().click();
  await expect(admin).toHaveURL(new RegExp(`/discovery/runs/${run}$`));
  await admin.context().close();
});
