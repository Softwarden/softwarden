import { expect, test, type Page } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

/** The Change assessments list, New assessment, the detail page with its stale banner, status menu and
 *  snapshot history, and retirement with acknowledgment from the edit form and from the status menu. Synthetic data only. */

const app = (page: Page, name: string, extra: object = {}) => post(page, "applications", { name, lifecycle: "active", ...extra });
const component = (page: Page, name: string, extra: object = {}) => post(page, "components", { name, lifecycle: "active", component_type: "script", ...extra });
const dependsOn = (page: Page, source: string, target: string) => post(page, "relationships", { type: "depends_on", source, target });

/** A target with a mid-chain dependent that has an owner, a dependent with none, a cycle back, a retired and an archived dependent and a data flow with an implementer. */
async function scenario(page: Page, t: string) {
  const priya = await personId(page, "Priya Raman");
  const core = await app(page, `${t} Core`);
  const mid = await app(page, `${t} Mid`, { technical_owner: priya });
  const top = await component(page, `${t} Top`);
  const retired = await app(page, `${t} Old`, { lifecycle: "retired" });
  const archived = await app(page, `${t} Shelved`);
  const other = await app(page, `${t} Partner`);
  await dependsOn(page, mid.id, core.id);
  await dependsOn(page, top.id, mid.id);
  await dependsOn(page, core.id, top.id); // a cycle back to the target
  await dependsOn(page, retired.id, core.id);
  await dependsOn(page, archived.id, core.id);
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: core.id, target: other.id, protocol: "SFTP" });
  const impl = await component(page, `${t} Courier`);
  await post(page, "relationships", { type: "implements", source: impl.id, target: flow.id });
  const assessment = await post(page, "assessments", { target: core.id, change_type: "change", title: `${t} Core upgrade`, target_date: "2027-02-01" });
  await post(page, `applications/${archived.id}/archive`, {});
  return { core, mid, top, retired, archived, flow, impl, assessment };
}

test("List: nav entry, rows with the Stale flag beside the status, filters and search in the URL, states", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { core, assessment } = await scenario(editor, t);
  await post(editor, "assessments", { target: core.id, change_type: "retirement", title: `${t} Retire Core`, status: "closed" });
  const extra = await app(editor, `${t} Newcomer`);
  await dependsOn(editor, extra.id, core.id); // stales the first assessment

  await editor.goto("/");
  await editor.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Change assessments" }).click();
  await expect(editor).toHaveURL(/\/assessments$/);
  await expect(editor.getByRole("heading", { name: "Change assessments", level: 1 })).toBeVisible();
  await expect(editor.getByRole("button", { name: "New assessment" })).toHaveCount(0); // a link, not a button
  await expect(editor.getByRole("link", { name: "New assessment" })).toBeVisible();

  await editor.getByLabel("Search assessments").fill(t);
  await expect(editor).toHaveURL(new RegExp(`q=${t}`));
  const table = editor.getByRole("table", { name: "Change assessments, most recent activity first" });
  const row = table.getByRole("row", { name: new RegExp(`${t} Core upgrade`) });
  await expect(row).toBeVisible();
  await expect(row.getByText("Draft", { exact: true })).toBeVisible();
  await expect(row.getByText("Stale snapshot")).toBeVisible(); // glyph and text beside the status, not a status itself
  await expect(row.getByText("Change", { exact: true })).toBeVisible();
  await expect(row.getByText("2027-02-01")).toBeVisible();
  await expect(row.getByText("Sam Editor")).toBeVisible();
  await expect(row.getByRole("link", { name: `${t} Core`, exact: true })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Retire Core`) })).toHaveCount(0); // Closed is not Open

  await editor.getByLabel("Status").selectOption("closed");
  await expect(editor).toHaveURL(/status=closed/);
  await expect(table.getByRole("row", { name: new RegExp(`${t} Retire Core`) })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Core upgrade`) })).toHaveCount(0);
  await editor.getByLabel("Status").selectOption("all");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Retire Core`) })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Core upgrade`) })).toBeVisible();

  await editor.getByLabel("Stale snapshots only").check();
  await expect(editor).toHaveURL(/stale=1/);
  await expect(table.getByRole("row", { name: new RegExp(`${t} Core upgrade`) })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Retire Core`) })).toHaveCount(0);

  await editor.reload(); // the filters survive a reload
  await expect(editor.getByLabel("Status")).toHaveValue("all");
  await expect(editor.getByLabel("Stale snapshots only")).toBeChecked();
  await editor.getByLabel("Search assessments").fill(`${t} nothing like this`);
  await expect(editor.getByText("No assessments match these filters")).toBeVisible();
  await editor.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(editor).toHaveURL(/\/assessments$/);

  await editor.goto(`/assessments?q=${t}`);
  await editor.getByRole("link", { name: `${t} Core upgrade` }).click();
  await expect(editor).toHaveURL(new RegExp(`/assessments/${assessment.id}$`));
  await expect(editor.getByRole("heading", { name: `${t} Core upgrade`, level: 1 })).toBeVisible();

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/assessments?q=${t}`);
  await expect(viewer.getByRole("row", { name: new RegExp(`${t} Core upgrade`) })).toBeVisible();
  await expect(viewer.getByRole("link", { name: "New assessment" })).toHaveCount(0);
  await viewer.goto("/assessments/new");
  await expect(viewer.getByRole("heading", { name: "You can't create assessments" })).toBeVisible();
});

test("List: loading, error with Retry, empty and no-match states", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  let mode: "slow" | "fail" | "empty" = "slow";
  await editor.route("**/api/v1/assessments?*", async (route) => {
    if (mode === "slow") { await new Promise((r) => setTimeout(r, 800)); return route.fulfill({ contentType: "application/json", body: "[]" }); }
    if (mode === "fail") return route.fulfill({ status: 500, contentType: "application/problem+json", body: JSON.stringify({ detail: "The server had a problem." }) });
    return route.fulfill({ contentType: "application/json", body: "[]" });
  });
  await editor.goto("/assessments");
  await expect(editor.getByText("Loading assessments…")).toBeVisible();
  await expect(editor.getByText("No open assessments")).toBeVisible();
  mode = "fail";
  await editor.reload();
  await expect(editor.getByRole("alert")).toContainText("Couldn't load assessments.");
  mode = "empty";
  await editor.getByRole("button", { name: "Retry" }).click();
  await expect(editor.getByText("No open assessments")).toBeVisible();
  await expect(editor.getByRole("link", { name: "New assessment" })).toHaveCount(2); // header and empty state
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.route("**/api/v1/assessments?*", (route) => route.fulfill({ contentType: "application/json", body: "[]" }));
  await viewer.goto("/assessments");
  await expect(viewer.getByText("Editors and Admins can start an assessment", { exact: false })).toBeVisible();
});

test("Start change assessment from Dependencies, then New assessment: validation, Calculating affected set and the saved snapshot", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { core } = await scenario(editor, t);
  await editor.goto("/dependencies");
  await expect(editor.getByRole("link", { name: "Start change assessment" })).toHaveCount(0); // no target yet
  await editor.goto(`/dependencies?target=${core.id}`);
  const start = editor.getByRole("link", { name: "Start change assessment" });
  await expect(start).toBeVisible();
  // The page's one filled button: the header link has the primary background and nothing else in the header does.
  await expect(start).toHaveClass(/bg-primary/);
  await start.click();
  await expect(editor).toHaveURL(new RegExp(`/assessments/new\\?target=${core.id}`));
  await expect(editor.getByRole("combobox", { name: "Target (required)" })).toHaveValue(`${t} Core`);

  await editor.getByRole("button", { name: "Create assessment" }).click();
  const summary = editor.getByRole("alert").filter({ hasText: "Fix these fields" });
  await expect(summary).toBeVisible();
  await expect(summary).toBeFocused();
  await expect(summary.getByRole("link", { name: "Choose change or retirement." })).toBeVisible();
  await summary.getByRole("link", { name: "Enter a title." }).click();
  await expect(editor.getByLabel("Title (required)")).toBeFocused();

  await editor.getByRole("radio", { name: "Retirement" }).check();
  await editor.getByLabel("Title (required)").fill(`${t} Retire Core`);
  await editor.getByLabel("Target date").fill("2027-03-01");
  await editor.getByLabel("External ticket URL").fill("https://tickets.example.test/CHG-1182");
  await editor.route("**/api/v1/assessments", async (route) => {
    if (route.request().method() === "POST") await new Promise((r) => setTimeout(r, 700));
    await route.continue();
  });
  await editor.getByRole("button", { name: "Create assessment" }).click();
  await expect(editor.getByRole("button", { name: "Calculating affected set…" })).toBeVisible();
  await expect(editor.getByRole("button", { name: "Calculating affected set…" })).toBeDisabled();
  await expect(editor).toHaveURL(/\/assessments\/[0-9a-f-]{36}$/);
  await expect(editor.getByRole("heading", { name: `${t} Retire Core`, level: 1 })).toBeVisible();
  await expect(editor.getByText(/Assessment created\. Affected set saved: 4 dependents, 1 data flow\./).first()).toBeVisible();
  await expect(editor.getByRole("list", { name: "Snapshot summary" })).toContainText("4 dependents");
  await expect(editor.getByRole("link", { name: /tickets\.example\.test\/CHG-1182/ })).toBeVisible();

  // An API field error (a ticket that isn't a web address) lands on the field and keeps everything typed.
  await editor.goto("/assessments/new");
  await editor.getByRole("combobox", { name: "Target (required)" }).fill(`${t} Mid`);
  await editor.getByRole("option", { name: new RegExp(`${t} Mid`) }).click();
  await editor.getByRole("radio", { name: "Change" }).check();
  await editor.getByLabel("Title (required)").fill("Keep me");
  await editor.getByLabel("External ticket URL").fill("ftp://nope");
  await editor.getByRole("button", { name: "Create assessment" }).click();
  await expect(editor.getByRole("link", { name: "Enter a full web address starting with http:// or https://." })).toBeVisible();
  await expect(editor.getByLabel("Title (required)")).toHaveValue("Keep me");
});

test("Detail: header, summary list, affected-set table with then-versus-now, tags, cycle row and a stale banner that refresh clears", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { core, mid, assessment } = await scenario(editor, t);
  await editor.goto(`/assessments/${assessment.id}`);
  await expect(editor.getByRole("heading", { name: `${t} Core upgrade`, level: 1 })).toBeVisible();
  await expect(editor.getByRole("button", { name: /retire/i })).toHaveCount(0); // no header Retire action
  await expect(editor.getByRole("status").filter({ hasText: "This snapshot is stale" })).toHaveCount(0);
  const refresh = editor.getByRole("button", { name: "Refresh snapshot" });
  await expect(refresh).not.toHaveClass(/bg-primary/); // not the primary while fresh

  const summary = editor.getByRole("list", { name: "Snapshot summary" });
  await expect(summary.getByRole("listitem").first()).toHaveText("4 dependents"); // Mid, Top, Old and Shelved; the cycle row is counted apart
  await expect(summary).toContainText("1 cycle");
  await expect(summary).toContainText("1 already Retired");
  await expect(editor.getByRole("heading", { name: /^Affected set — / })).toContainText("data flow");
  const table = editor.getByRole("table", { name: /^Affected set for/ });
  await expect(table.getByRole("columnheader", { name: "Lifecycle then" })).toBeVisible();
  const midRow = table.getByRole("row", { name: new RegExp(`${t} Mid`) }).first();
  await expect(midRow).toContainText("Priya Raman");
  await expect(midRow).toContainText(`${t} Mid → depends on →`);
  await expect(table.getByRole("columnheader", { name: "Link status" })).toBeVisible();
  await expect(midRow).toContainText("Unconfirmed"); // the link's state now, beside whether it changed
  await expect(midRow).toContainText("Never confirmed.");
  await expect(midRow).toContainText("Unchanged since the snapshot");
  const oldRow = table.getByRole("row", { name: new RegExp(`${t} Old`) });
  await expect(oldRow.getByText("Retired", { exact: true }).first()).toBeVisible();
  const shelved = table.getByRole("row", { name: new RegExp(`${t} Shelved`) });
  await expect(shelved.getByText("Archived", { exact: true })).toBeVisible();
  const cycle = table.locator("tr", { hasText: "Cycle back to" });
  await expect(cycle).toContainText(`${t} Core`);
  await expect(cycle.getByRole("rowheader")).toContainText("Cycle");
  await expect(table.getByRole("rowgroup").filter({ hasText: "Data flows — data flow, not operational" })).toBeVisible();
  const flowRow = table.getByRole("row", { name: new RegExp(`${t} Flow`) });
  await expect(flowRow).toContainText("Implemented by");
  await expect(flowRow).toContainText(`${t} Courier`);
  await expect(flowRow).toContainText(`${t} Core`);
  await expect(editor.getByText("A missing path is not evidence that a change is safe.")).toBeVisible();

  // The catalog moves on: a lifecycle change on a dependent and a new dependent make the snapshot stale.
  await post(editor, `applications/${mid.id}`, { lifecycle: "deprecated" }, "PATCH");
  const newcomer = await app(editor, `${t} Newcomer`);
  await dependsOn(editor, newcomer.id, core.id);
  await editor.reload();
  const banner = editor.getByRole("status").filter({ hasText: "This snapshot is stale" });
  await expect(banner).toBeVisible();
  await expect(banner.getByRole("list", { name: "Why this snapshot is stale" }).getByRole("listitem")).toHaveCount(2);
  await expect(banner).toContainText("lifecycle changed (Active to Deprecated)");
  await expect(banner).toContainText("A depends_on link was added");
  await expect(banner).toContainText("Nothing refreshes on its own.");
  await expect(editor.getByText("Stale snapshot").first()).toBeVisible(); // the flag beside the status in Details
  await expect(refresh).toHaveClass(/bg-primary/); // the header primary while stale
  await expect(midRow).toContainText("Changed. Now Deprecated");
  await expect(midRow.getByText("Link changed")).toHaveCount(0);

  await refresh.click();
  await expect(editor.getByText(/Snapshot refreshed: 5 dependents, 1 data flow\./).first()).toBeVisible();
  await expect(banner).toHaveCount(0);
  await expect(refresh).not.toHaveClass(/bg-primary/);
  await expect(refresh).toBeFocused();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Newcomer`) })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Mid`) }).first()).toContainText("Deprecated");

  // History: both snapshots are listed, the earlier one is read-only and has its own URL state.
  await editor.getByRole("button", { name: "Snapshot history (2)" }).click();
  const history = editor.getByRole("table", { name: /^Snapshot history/ });
  await expect(history.getByRole("row")).toHaveCount(3); // header and two snapshots
  await expect(history.getByText("Current", { exact: true })).toBeVisible();
  await expect(history.getByText("Stale snapshot")).toBeVisible(); // the earlier snapshot stays stale
  await history.getByRole("button", { name: /^View snapshot taken/ }).click();
  await expect(editor).toHaveURL(/snapshot=/);
  await expect(editor.getByText(/You are viewing the snapshot taken/)).toBeVisible();
  await expect(editor.getByRole("table", { name: /^Affected set for/ }).getByRole("row", { name: new RegExp(`${t} Newcomer`) })).toHaveCount(0);
  await editor.getByRole("button", { name: "Back to the current snapshot" }).click();
  await expect(editor).not.toHaveURL(/snapshot=/);
  await expect(editor.getByRole("table", { name: /^Affected set for/ }).getByRole("row", { name: new RegExp(`${t} Newcomer`) })).toBeVisible();
});

test("Detail: Change status is a menu with a confirm step, and Edit details saves", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const core = await app(editor, `${t} Solo`);
  const a = await post(editor, "assessments", { target: core.id, change_type: "change", title: `${t} Solo change` });
  await editor.goto(`/assessments/${a.id}`);
  await expect(editor.getByRole("combobox", { name: "Status" })).toHaveCount(0); // not a <select>
  await expect(editor.getByText("Nothing is recorded as depending on")).toBeVisible();

  const menu = editor.getByRole("button", { name: /^Change status/ });
  await menu.focus();
  await editor.keyboard.press("ArrowDown");
  await expect(editor.getByRole("menuitem", { name: "Draft" })).toHaveCount(0); // the current status isn't offered
  await expect(editor.getByRole("menuitem", { name: "In review" })).toBeFocused();
  await editor.keyboard.press("Escape");
  await expect(menu).toBeFocused();
  await menu.click();
  await editor.getByRole("menuitem", { name: "In review" }).click();
  const dialog = editor.getByRole("dialog", { name: "Change status to In review?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(menu).toBeFocused(); // focus returns to the opener
  await expect(editor.getByText("Draft", { exact: true }).first()).toBeVisible(); // nothing changed

  await menu.click();
  await editor.getByRole("menuitem", { name: "In review" }).click();
  await editor.getByRole("dialog").getByRole("button", { name: "Change status" }).click();
  await expect(editor.getByText("Status changed to In review.").first()).toBeVisible();
  await expect(editor.getByText("In review", { exact: true }).first()).toBeVisible();
  await expect(menu).toBeFocused();

  const edit = editor.getByRole("button", { name: "Edit", exact: true });
  await edit.click();
  await editor.getByLabel("Title (required)").fill("");
  await editor.getByRole("button", { name: "Save details" }).click();
  await expect(editor.getByRole("dialog").getByRole("link", { name: "Enter a title." })).toBeVisible();
  await editor.getByLabel("Title (required)").fill(`${t} Renamed`);
  await editor.getByLabel("External ticket URL").fill("https://tickets.example.test/CHG-9");
  await editor.getByRole("button", { name: "Save details" }).click();
  await expect(editor.getByRole("heading", { name: `${t} Renamed`, level: 1 })).toBeVisible();
  await expect(edit).toBeFocused();
});

test("Detail: Description and Findings are one form with one Save that keeps the text when it fails; Viewers read only", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const core = await app(editor, `${t} Notes`);
  const a = await post(editor, "assessments", { target: core.id, change_type: "change", title: `${t} Notes change`, description: "Original description" });
  await editor.goto(`/assessments/${a.id}`);
  const form = editor.getByRole("form", { name: "Description and findings" });
  await expect(form.getByRole("button", { name: "Save" })).toHaveCount(1);
  await expect(form.getByLabel("Description")).toHaveValue("Original description");
  await form.getByLabel("Findings").fill("Kiosk vendor confirmed replacement by 2026-12-01");
  await form.getByLabel("Description").fill("Edited description");

  let fail = true;
  await editor.route(`**/api/v1/assessments/${a.id}`, async (route) => {
    if (route.request().method() === "PATCH" && fail) return route.fulfill({ status: 500, contentType: "application/problem+json", body: JSON.stringify({ detail: "The server had a problem." }) });
    return route.continue();
  });
  await form.getByRole("button", { name: "Save" }).click();
  await expect(editor.getByRole("alert").filter({ hasText: "Your text wasn't saved" })).toBeVisible();
  await expect(form.getByLabel("Findings")).toHaveValue("Kiosk vendor confirmed replacement by 2026-12-01");
  await expect(form.getByLabel("Description")).toHaveValue("Edited description");

  fail = false;
  await form.getByRole("button", { name: "Save" }).click();
  await expect(editor.getByText("Description and findings saved.").first()).toBeVisible();
  await editor.unroute(`**/api/v1/assessments/${a.id}`);
  await editor.reload();
  await expect(editor.getByLabel("Findings", { exact: true })).toHaveValue("Kiosk vendor confirmed replacement by 2026-12-01");

  // Unsaved text asks before an in-app link discards it.
  await editor.getByLabel("Findings", { exact: true }).fill("Unsaved");
  await editor.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Change assessments" }).click();
  await expect(editor.getByRole("dialog", { name: "Discard changes?" })).toBeVisible();
  await editor.getByRole("button", { name: "Keep editing" }).click();
  await expect(editor.getByLabel("Findings", { exact: true })).toHaveValue("Unsaved");

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/assessments/${a.id}`);
  await expect(viewer.getByRole("heading", { name: `${t} Notes change`, level: 1 })).toBeVisible();
  await expect(viewer.getByText("Kiosk vendor confirmed replacement by 2026-12-01")).toBeVisible();
  for (const name of ["Refresh snapshot", "Save", "Edit"]) await expect(viewer.getByRole("button", { name, exact: true })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Change status/ })).toHaveCount(0);
  await expect(viewer.getByRole("textbox")).toHaveCount(0);
  await post(editor, "relationships", { type: "depends_on", source: (await app(editor, `${t} Late`)).id, target: core.id });
  await viewer.reload();
  await expect(viewer.getByRole("status").filter({ hasText: "An Editor or Admin can refresh it." })).toBeVisible();
});

test("Detail: loading, error with Retry and not found", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const missing = "00000000-0000-4000-8000-000000000000";
  await editor.goto(`/assessments/${missing}`);
  await expect(editor.getByRole("heading", { name: "Assessment not found" })).toBeVisible();
  await editor.route("**/api/v1/assessments/*", async (route) => {
    await new Promise((r) => setTimeout(r, 600));
    await route.fulfill({ status: 500, contentType: "application/problem+json", body: JSON.stringify({ detail: "The server had a problem." }) });
  });
  await editor.goto(`/assessments/${missing.replace(/0$/, "1")}`);
  await expect(editor.getByText("Loading the assessment…")).toBeVisible();
  await expect(editor.getByRole("alert")).toContainText("Couldn't load this assessment.");
  await expect(editor.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("Retirement assessment to Ready for external execution asks for the acknowledgment and shows the reason afterwards", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const core = await app(editor, `${t} Timeclock`);
  const kiosk = await app(editor, `${t} Kiosk`);
  const old = await app(editor, `${t} Reports`, { lifecycle: "retired" });
  await dependsOn(editor, kiosk.id, core.id);
  await dependsOn(editor, old.id, core.id);
  const a = await post(editor, "assessments", { target: core.id, change_type: "retirement", title: `${t} Retire Timeclock` });
  await editor.goto(`/assessments/${a.id}`);
  const menu = editor.getByRole("button", { name: /^Change status/ });
  await menu.click();
  await editor.getByRole("menuitem", { name: "Ready for external execution" }).click();
  const dialog = editor.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Change status to Ready for external execution?" })).toBeVisible();
  await expect(dialog).toContainText("This assessment is for a retirement");
  await dialog.getByRole("button", { name: "Change status" }).click();
  await expect(dialog.getByRole("heading", { name: `Mark the retirement of ${t} Timeclock ready?` })).toBeVisible(); // the same dialog, now asking
  await expect(dialog.getByRole("list", { name: "Active dependents" })).toContainText(`${t} Kiosk`);
  await expect(dialog).toContainText(`${t} Reports is Retired and doesn't need acknowledgment.`);
  await expect(dialog).toContainText("It doesn't retire");
  const confirm = dialog.getByRole("button", { name: "Acknowledge and mark ready" });
  await expect(confirm).toHaveAttribute("aria-disabled", "true");
  await dialog.getByLabel(/^Reason \(required/).fill("Kiosk moves to the new vendor API");
  await dialog.getByRole("checkbox", { name: "I acknowledge 1 active dependent" }).check();
  await confirm.click();
  await expect(editor.getByText(/Status changed to Ready for external execution\. Retirement acknowledged/).first()).toBeVisible();
  await expect(editor.getByText("Retirement acknowledgment", { exact: true })).toBeVisible();
  await expect(editor.getByText("Kiosk moves to the new vendor API")).toBeVisible();
  await expect(menu).toBeFocused();
  expect((await (await editor.request.get(`/api/v1/applications/${core.id}`)).json()).lifecycle).toBe("active"); // the assessment doesn't retire it

  // Moving on from Ready never asks again.
  await menu.click();
  await editor.getByRole("menuitem", { name: "In review" }).click();
  await editor.getByRole("dialog").getByRole("button", { name: "Change status" }).click();
  await expect(editor.getByText("Status changed to In review.").first()).toBeVisible();
});
