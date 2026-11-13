import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Saved views and their weekly emails on the record lists. Save the current filters and columns as a
 *  view, reopen it with `?view=`, a link of the weekly email's shape (`/applications?<query>&preset=all&view=<id>`)
 *  showing exactly the API's rows for the view (cf_ filters and components included), rename / share / delete with
 *  If-Match (412), private versus shared views between members, Admins' subscribers list, "Email me weekly" with the
 *  send time in the organization's timezone, a test send (409 and 429 inline), the "Saved view emails" preference, a
 *  Viewer's limits, and the acceptance check: the weekly run sends one email on the chosen weekday with the view's rows and a
 *  link that reproduces the filter. axe in both themes.
 *
 *  The weekly run is organization-wide, so the spec has its own organization (`e2e/spec-org`, `views`: Pacific/Auckland,
 *  digest time 08:00), its tests in order. Email goes to the e2e server's in-memory outbox (`e2e/run-digest` with
 *  `subscriptions`); nothing leaves the machine. */

const ADMIN = "sv-admin@example.test";
const EDITOR = "sv-editor@example.test";
const VIEWER = "sv-viewer@example.test";
const SCHEDULE_TZ = "08:00 Pacific/Auckland";

test.describe.configure({ mode: "serial" });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "views" });
    await contoso.context().close();
    // A custom field the views filter on (cf_sv_tier), once per run.
    const admin = await pageAs(browser, baseURL, ADMIN);
    const fields: { name: string }[] = await (await admin.request.get("/api/v1/custom-fields")).json();
    if (!fields.some((f) => f.name === "sv_tier")) {
      await post(admin, "custom-fields", { name: "sv_tier", label: "Tier", type: "select", choices: ["Gold", "Silver"], applies_to: ["application", "component"] });
    }
    await admin.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const said = (page: Page, text: string | RegExp) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeAttached();
/** The record names a list shows, in order. */
const shown = (page: Page, caption: string) => page.getByRole("region", { name: `${caption} table` }).getByRole("rowheader").allInnerTexts()
  .then((x) => x.map((s) => s.trim().split("\n")[0].trim()));
/** The record names the API's list gives for a query, in its order. */
async function apiRows(page: Page, path: string, query: Record<string, string>) {
  const page1: { results: { name: string }[] } = await (await page.request.get(`/api/v1/${path}?${new URLSearchParams(query)}`)).json();
  return page1.results.map((r) => r.name);
}
type View = { id: string; name: string; query: Record<string, string>; columns: string[]; shared: boolean };
const getView = async (page: Page, id: string): Promise<View> => (await page.request.get(`/api/v1/saved-filters/${id}`)).json();
const viewIdOf = (page: Page) => new URL(page.url()).searchParams.get("view") ?? "";

type Sent = { to: string[]; subject: string; body: string; html: string };
async function runWeekly(admin: Page, weekday: number): Promise<{ date: string; emails: Sent[] }> {
  return post(admin, "e2e/run-digest", { subscriptions: { weekday } });
}

test("Save a view with its filters and columns; reopen it with ?view=; a link of the email's shape shows the API's rows", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  await post(page, "applications", { name: `${t} Alpha`, lifecycle: "active", criticality: "high", custom_field_data: { sv_tier: "Gold" } });
  await post(page, "applications", { name: `${t} Bravo`, lifecycle: "active", custom_field_data: { sv_tier: "Silver" } });
  await post(page, "applications", { name: `${t} Charlie`, lifecycle: "retired", custom_field_data: { sv_tier: "Gold" } });
  await post(page, "applications", { name: `${t} Delta`, lifecycle: "active", custom_field_data: { sv_tier: "Gold" } });

  await page.goto(`/applications?q=${t}&lifecycle=active&cf_sv_tier=Gold`);
  const table = page.getByRole("region", { name: "Applications table" });
  await expect.poll(() => shown(page, "Applications")).toEqual([`${t} Alpha`, `${t} Delta`]);
  // Show Criticality as well.
  await page.getByRole("button", { name: /^Columns/ }).click();
  await page.getByRole("menu", { name: "Show columns" }).getByRole("menuitemcheckbox", { name: "Criticality" }).click();
  await page.keyboard.press("Escape");
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toBeVisible();

  await page.getByRole("button", { name: "Save as view…" }).click();
  const dialog = page.getByRole("dialog", { name: "Save as view" });
  await expect(dialog).toContainText("Tier: Gold");
  await expect(dialog).toContainText(`Search: ${t}`);
  await expect(dialog).toContainText(/Columns\s*Name, Lifecycle, Criticality, Review state, Gaps/);
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Enter a name.");
  await expectNoViolations(page, "Save as view dialog with an error");
  const name = `Gold ${t}`;
  await dialog.getByLabel("Name (required)").fill(name);
  await dialog.getByRole("button", { name: "Save view" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save as view…" })).toBeFocused();
  await said(page, `Saved view “${name}”.`);
  await expect(page).toHaveURL(/[?&]view=[0-9a-f-]{36}/);
  await expect(page).toHaveURL(/preset=all/);
  const id = viewIdOf(page);
  const select = page.getByLabel("Saved views", { exact: true });
  await expect(select).toHaveValue(id);
  await expect(select.locator("optgroup[label='My views'] option", { hasText: name })).toHaveCount(1);
  const open = page.getByRole("region", { name: `View: ${name}` });
  await expect(open).toContainText("Only you");
  const saved = await getView(page, id);
  expect(saved.query).toEqual({ q: t, lifecycle: "active", cf_sv_tier: "Gold" });
  expect(saved.columns.slice(0, 5)).toEqual(["name", "lifecycle", "criticality", "review_state", "gaps"]);
  await expectNoViolations(page, "Applications list with a saved view open");

  // Changing the columns marks the view changed; Save changes is offered to its owner.
  await page.getByRole("button", { name: /^Columns/ }).click();
  await page.getByRole("menuitem", { name: "Reset to default columns" }).click();
  await page.keyboard.press("Escape");
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toHaveCount(0);
  await expect(open).toContainText("Changed since saved: columns");
  await expect(open.getByRole("button", { name: `Save changes to “${name}”` })).toBeVisible();
  // Reopen saved version puts its columns back.
  await open.getByRole("button", { name: "Reopen saved version" }).click();
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toBeVisible();
  await expect(open).not.toContainText("Changed since saved");
  await page.getByRole("button", { name: /^Columns/ }).click();
  await page.getByRole("menuitem", { name: "Reset to default columns" }).click();
  await page.keyboard.press("Escape");
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toHaveCount(0);

  // Elsewhere, then ?view= alone: the view's query goes into the URL, its columns apply, and it is marked open.
  await page.goto("/applications");
  await page.goto(`/applications?view=${id}`);
  await expect(page).toHaveURL(/cf_sv_tier=Gold/);
  await expect(page).toHaveURL(/preset=all/);
  await expect(page.getByLabel("Search applications")).toHaveValue(t);
  await expect(select).toHaveValue(id);
  await expect(table.getByRole("columnheader", { name: "Criticality" })).toBeVisible();
  await expect(open).not.toContainText("Changed since saved");
  await expect.poll(() => shown(page, "Applications")).toEqual([`${t} Alpha`, `${t} Delta`]);
  // The bare link was replaced, not added to the history: Back goes to the page before it.
  await page.goBack();
  await expect(page).toHaveURL(/\/applications$/);

  // The email's link: the list route, the view's query, preset=all and view=<id>. Same rows as the API gives.
  const link = `/applications?${new URLSearchParams({ ...saved.query, preset: "all", view: id })}`;
  await page.goto("/applications");
  await page.goto(link);
  const expected = await apiRows(page, "applications", saved.query);
  expect(expected).toEqual([`${t} Alpha`, `${t} Delta`]);
  await expect.poll(() => shown(page, "Applications")).toEqual(expected);
  await expect(select).toHaveValue(id);

  // The same filters without view=: the selector names the view, but nothing is opened for writing.
  await page.goto(`/applications?${new URLSearchParams({ ...saved.query, preset: "all" })}`);
  await expect(select).toHaveValue(id);
  await expect(open).toHaveCount(0);

  // A built-in isn't marked by ID: changing a filter by hand leaves it.
  await select.selectOption({ label: "Active" });
  await expect(page).toHaveURL(/lifecycle=active%2Cdeprecated/);
  await expect(page).not.toHaveURL(/view=/);
  await expect(select).toHaveValue("builtin:active");
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Criticality", exact: true }).click();
  await page.getByLabel("Criticality", { exact: true }).selectOption({ label: "High" });
  await expect(select).toHaveValue("");
});

test("A components view with a cf_ filter is emailed once on the chosen weekday, with its rows and a link that opens the same rows", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  await post(page, "components", { name: `${t} Job A`, lifecycle: "active", component_type: "script", custom_field_data: { sv_tier: "Gold" } });
  await post(page, "components", { name: `${t} Job B`, lifecycle: "active", component_type: "script", custom_field_data: { sv_tier: "Silver" } });
  await page.goto(`/applications?kind=components&q=${t}&cf_sv_tier=Gold&preset=all`);
  await expect.poll(() => shown(page, "Components")).toEqual([`${t} Job A`]);
  await page.getByRole("button", { name: "Save as view…" }).click();
  const name = `Gold jobs ${t}`;
  await page.getByRole("dialog", { name: "Save as view" }).getByLabel("Name (required)").fill(name);
  await page.getByRole("dialog", { name: "Save as view" }).getByRole("button", { name: "Save view" }).click();
  await said(page, `Saved view “${name}”.`);
  await expect(page).toHaveURL(/kind=components.*view=[0-9a-f-]{36}/);
  const id = viewIdOf(page);

  // Email me weekly on Wednesday: the send time in the organization's timezone.
  await page.getByRole("button", { name: "Email me weekly…" }).click();
  const dialog = page.getByRole("dialog", { name: `Weekly email: ${name}` });
  await dialog.getByLabel("Day").selectOption({ label: "Wednesday" });
  await expect(dialog.getByTestId("sub-schedule")).toHaveText(`Sent Wednesdays at ${SCHEDULE_TZ}`);
  await expectNoViolations(page, "Weekly email dialog, before subscribing");
  await dialog.getByRole("button", { name: "Subscribe" }).click();
  await expect(dialog).toHaveCount(0);
  await said(page, `Subscribed to “${name}”: Sent Wednesdays at ${SCHEDULE_TZ}.`);
  await expect(page.getByRole("region", { name: `View: ${name}` })).toContainText("Weekly email: Wednesdays");

  // The weekly run on another weekday sends nothing for it; on Wednesday, one email to the subscriber.
  const admin = await as(browser, baseURL, ADMIN);
  const mine = (emails: Sent[]) => emails.filter((e) => e.to.includes(EDITOR) && e.body.includes(name));
  expect(mine((await runWeekly(admin, 3)).emails)).toHaveLength(0);
  const wednesday = await runWeekly(admin, 2);
  expect(new Date(`${wednesday.date}T12:00:00Z`).getUTCDay()).toBe(3); // a Wednesday
  const emails = mine(wednesday.emails);
  expect(emails).toHaveLength(1);
  const [email] = emails;
  expect(email.body).toContain(`${t} Job A`);
  expect(email.body).not.toContain(`${t} Job B`);
  expect(email.body).toContain("1 component match");
  const link = /Open the view in Softwarden: (\S+)/.exec(email.body)?.[1] ?? "";
  const url = new URL(link);
  expect(url.pathname).toBe("/applications");
  expect(Object.fromEntries(url.searchParams)).toEqual({ kind: "components", q: t, cf_sv_tier: "Gold", preset: "all", view: id });
  expect(email.html).toContain(`${t} Job A`);
  // Once a day: a second run on the same date sends nothing.
  expect(mine((await runWeekly(admin, 2)).emails)).toHaveLength(0);

  // The link opens the list with exactly the email's (and the API's) rows, the view marked open.
  await page.goto("/applications");
  await page.goto(`${url.pathname}${url.search}`);
  const expected = await apiRows(page, "components", (await getView(page, id)).query);
  expect(expected).toEqual([`${t} Job A`]);
  await expect.poll(() => shown(page, "Components")).toEqual(expected);
  await expect(page.getByLabel("Saved views", { exact: true })).toHaveValue(id);
  // The subscription shows the send.
  await page.getByRole("button", { name: "Weekly email…" }).click();
  await expect(page.getByRole("dialog", { name: `Weekly email: ${name}` }).getByTestId("sub-last-sent")).toHaveText(wednesday.date);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Weekly email…" })).toBeFocused();

  // The view's ID on another list isn't opened there; it says where it opens.
  await page.goto(`/applications?view=${id}`);
  const elsewhere = page.getByTestId("view-elsewhere");
  await expect(elsewhere).toContainText(`“${name}” is a view of components`);
  await expect(page.getByRole("region", { name: `View: ${name}` })).toHaveCount(0);
  await elsewhere.getByRole("link", { name: "Open it on Components" }).click();
  await expect(page).toHaveURL(/kind=components.*cf_sv_tier=Gold/);
  await expect(page.getByRole("region", { name: `View: ${name}` })).toBeVisible();
});

test("An AI agents view: a link of the email's shape shows the API's rows", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  await post(page, "ai-agents", { name: `${t} Writer`, lifecycle: "active", actions_can_write: true });
  await post(page, "ai-agents", { name: `${t} Reader`, lifecycle: "active", actions_can_write: false });
  await post(page, "ai-agents", { name: `${t} Old writer`, lifecycle: "retired", actions_can_write: true });
  const view: View = await post(page, "saved-filters", { name: `Writers ${t}`, kind: "ai_agent", query: { q: t, lifecycle: "active", actions_can_write: "true" } });
  const link = `/ai-agents?${new URLSearchParams({ ...view.query, preset: "all", view: view.id })}`;
  await page.goto(link);
  const expected = await apiRows(page, "ai-agents", view.query);
  expect(expected).toEqual([`${t} Writer`]);
  await expect.poll(() => shown(page, "AI agents")).toEqual(expected);
  await expect(page.getByLabel("Saved views", { exact: true })).toHaveValue(view.id);
  await expect(page.getByRole("region", { name: `View: ${view.name}` })).toBeVisible();
  await expectNoViolations(page, "AI agents list with a saved view open");
});

test("Subscribe for a weekday, send a test (409 with emails off, 429 right after), change the day, turn it off and unsubscribe", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  const t = token();
  const view: View = await post(page, "saved-filters", { name: `Viewer ${t}`, kind: "application", query: { lifecycle: "active" } });
  await page.goto(`/applications?view=${view.id}`);
  const open = page.getByRole("region", { name: `View: ${view.name}` });
  await open.getByRole("button", { name: "Email me weekly…" }).click();
  let dialog = page.getByRole("dialog", { name: `Weekly email: ${view.name}` });
  await dialog.getByLabel("Day").selectOption({ label: "Friday" });
  await dialog.getByRole("button", { name: "Subscribe" }).click();
  await said(page, `Subscribed to “${view.name}”: Sent Fridays at ${SCHEDULE_TZ}.`);
  await expect(open).toContainText("Weekly email: Fridays");
  // Cancel discards a changed day: reopened, the dialog shows the saved one.
  await open.getByRole("button", { name: "Weekly email…" }).click();
  await expect(dialog.getByLabel("Day")).toHaveValue("4");
  await dialog.getByLabel("Day").selectOption({ label: "Tuesday" });
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  await open.getByRole("button", { name: "Weekly email…" }).click();
  await expect(dialog.getByLabel("Day")).toHaveValue("4");
  await expect(dialog.getByTestId("sub-schedule")).toHaveText(`Sent Fridays at ${SCHEDULE_TZ}`);
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // Saved view emails off in the profile: the dialog says so and a test is refused with the reason (409).
  await page.goto("/profile");
  const pref = page.getByRole("checkbox", { name: "Saved view emails" });
  await expect(pref).toBeChecked();
  await expect(page.getByRole("table", { name: "Your weekly saved view emails" })).toContainText(view.name);
  await expectNoViolations(page, "Profile with saved view emails");
  await pref.uncheck();
  await said(page, "Saved view emails turned off.");
  await page.goto(`/applications?view=${view.id}`);
  await page.getByRole("button", { name: "Weekly email…" }).click();
  dialog = page.getByRole("dialog", { name: `Weekly email: ${view.name}` });
  await expect(dialog).toContainText("Saved view emails are turned off in your profile");
  await expect(dialog.getByTestId("sub-last-sent")).toHaveText("Not sent yet");
  await dialog.getByRole("button", { name: "Send me a test now" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Can't send: the member turned off saved view emails.");
  await expectNoViolations(page, "Weekly email dialog with a refused test");
  await page.keyboard.press("Escape");

  // On again: the test is sent; a second one within a minute is refused (429) with when to retry.
  await post(page, "auth/me/preferences", { subscriptions_enabled: true }, "PATCH");
  await page.getByRole("button", { name: "Weekly email…" }).click();
  await expect(dialog).not.toContainText("Saved view emails are turned off");
  await dialog.getByRole("button", { name: "Send me a test now" }).click();
  await expect(dialog.getByRole("status")).toContainText("Test email sent to you.");
  await dialog.getByRole("button", { name: "Send me a test now" }).click();
  await expect(dialog.getByRole("alert")).toContainText(/A test email for this subscription was just sent\. Try again in \d+ seconds\./);
  await expectNoViolations(page, "Weekly email dialog after a test");

  // Change the day and turn it off.
  await dialog.getByLabel("Day").selectOption({ label: "Monday" });
  await expect(dialog.getByTestId("sub-schedule")).toHaveText(`Sent Mondays at ${SCHEDULE_TZ}`);
  await dialog.getByLabel("Send the weekly email").uncheck();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toHaveCount(0);
  await said(page, `Weekly email of “${view.name}” turned off.`);
  await expect(open).toContainText("Weekly email: Mondays (turned off)");
  const subs: { weekday: number; active: boolean }[] = await (await page.request.get(`/api/v1/saved-view-subscriptions?saved_view=${view.id}`)).json();
  expect(subs).toEqual([expect.objectContaining({ weekday: 0, active: false })]);

  // Unsubscribe.
  await open.getByRole("button", { name: "Weekly email…" }).click();
  await dialog.getByRole("button", { name: "Unsubscribe" }).click();
  await said(page, `Unsubscribed from “${view.name}”.`);
  await expect(open.getByRole("button", { name: "Email me weekly…" })).toBeVisible();
});

test("Private and shared views between members: a private view is the owner's alone; sharing, Admin subscribers, 412 and field errors, delete", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  const viewer = await as(browser, baseURL, VIEWER);
  const admin = await as(browser, baseURL, ADMIN);
  const t = token();
  const view: View = await post(editor, "saved-filters", { name: `Team ${t}`, kind: "application", query: { lifecycle: "active", q: t } });

  // Private: nobody else sees it, Admins included (404); its link says it isn't available and shows the link's filters.
  for (const other of [viewer, admin]) {
    expect((await other.request.get(`/api/v1/saved-filters/${view.id}`)).status()).toBe(404);
    await other.goto(`/applications?lifecycle=active&q=${t}&preset=all&view=${view.id}`);
    await expect(other.getByTestId("view-unavailable")).toContainText("isn't available to you");
    await expect(other.getByLabel("Saved views", { exact: true }).locator("option", { hasText: view.name })).toHaveCount(0);
    await expect(other.getByLabel("Search applications")).toHaveValue(t);
  }
  await expectNoViolations(admin, "A link to a view that isn't available");

  // The owner shares it.
  await editor.goto(`/applications?view=${view.id}`);
  const open = editor.getByRole("region", { name: `View: ${view.name}` });
  await open.getByRole("button", { name: "Rename or share…" }).click();
  const edit = editor.getByRole("dialog", { name: `Rename or share “${view.name}”` });
  await edit.getByLabel("Share with everyone").check();
  await expectNoViolations(editor, "Rename or share dialog");
  await edit.getByRole("button", { name: "Save" }).click();
  await said(editor, `Saved “${view.name}”, shared with everyone.`);
  await expect(open).toContainText("Shared with everyone");
  await expect(open).toContainText("Owner: Sol Editor");

  // Now everyone sees it; a Viewer can't change it but can subscribe.
  await viewer.goto("/applications");
  await expect(viewer.getByLabel("Saved views", { exact: true }).locator("optgroup[label='Shared with everyone'] option", { hasText: view.name })).toHaveCount(1);
  await viewer.getByLabel("Saved views", { exact: true }).selectOption({ label: view.name });
  const seen = viewer.getByRole("region", { name: `View: ${view.name}` });
  await expect(seen).toContainText("Only its owner (an Editor or Admin) or an Admin can change this shared view.");
  await expect(seen.getByRole("button", { name: "Rename or share…" })).toHaveCount(0);
  await expect(seen.getByRole("button", { name: "Delete view…" })).toHaveCount(0);
  await seen.getByRole("button", { name: "Email me weekly…" }).click();
  await viewer.getByRole("dialog", { name: `Weekly email: ${view.name}` }).getByLabel("Day").selectOption({ label: "Thursday" });
  await viewer.getByRole("dialog", { name: `Weekly email: ${view.name}` }).getByRole("button", { name: "Subscribe" }).click();
  await said(viewer, `Subscribed to “${view.name}”`);

  // An Admin sees the subscribers, subscribes another member and removes one.
  await admin.goto(`/applications?view=${view.id}`);
  await admin.getByRole("region", { name: `View: ${view.name}` }).getByRole("button", { name: "Subscribers…" }).click();
  const subscribers = admin.getByRole("dialog", { name: `Subscribers: ${view.name}` });
  const rows = subscribers.getByRole("table", { name: `Subscribers to ${view.name}` });
  await expect(rows.getByRole("row", { name: /Sky Viewer/ })).toContainText("Thursday");
  await subscribers.getByLabel("Member").selectOption({ label: `Sol Editor (${EDITOR})` });
  await subscribers.getByLabel("Day").selectOption({ label: "Tuesday" });
  await subscribers.getByRole("button", { name: "Subscribe member" }).click();
  await expect(subscribers.getByRole("status")).toContainText(`Sol Editor subscribed: Sent Tuesdays at ${SCHEDULE_TZ}.`);
  await expect(rows.getByRole("row", { name: /Sol Editor/ })).toContainText("Tuesday");
  await expectNoViolations(admin, "Subscribers dialog");
  await rows.getByRole("button", { name: "Remove Sky Viewer" }).click();
  await expect(subscribers.getByRole("status")).toContainText("Sky Viewer unsubscribed.");
  await expect(rows.getByRole("row", { name: /Sky Viewer/ })).toHaveCount(0);
  await admin.keyboard.press("Escape");

  // 412: someone else changed the view after the owner opened it; Load the latest version, then save again.
  await editor.goto(`/applications?view=${view.id}`);
  await expect(open).toContainText("Shared with everyone");
  const renamed = `Team renamed ${t}`;
  await post(admin, `saved-filters/${view.id}`, { name: renamed }, "PATCH");
  await editor.getByRole("button", { name: "Add filter" }).click();
  await editor.getByRole("menuitem", { name: "Criticality", exact: true }).click();
  await editor.getByLabel("Criticality", { exact: true }).selectOption({ label: "High" });
  await expect(open).toContainText("Changed since saved: filters");
  await open.getByRole("button", { name: `Save changes to “${view.name}”` }).click();
  const stale = open.getByRole("alert").filter({ hasText: "Someone else changed this view after you opened it" });
  await expect(stale).toBeVisible();
  await expectNoViolations(editor, "Saved view refused with 412");
  await stale.getByRole("button", { name: "Load the latest version" }).click();
  const reopened = editor.getByRole("region", { name: `View: ${renamed}` });
  await expect(reopened).toBeVisible();
  await reopened.getByRole("button", { name: `Save changes to “${renamed}”` }).click();
  await said(editor, `Saved changes to “${renamed}”.`);
  expect((await getView(editor, view.id)).query).toEqual({ lifecycle: "active", q: t, criticality: "high" });
  await expect(reopened).not.toContainText("Changed since saved");

  // Field errors: a name already taken; the query allow-list explained in words.
  await reopened.getByRole("button", { name: "Rename or share…" }).click();
  const rename = editor.getByRole("dialog", { name: `Rename or share “${renamed}”` });
  await rename.getByLabel("Name (required)").fill("Active");
  await rename.getByRole("button", { name: "Save" }).click();
  await expect(rename.getByRole("alert")).toContainText("A filter with this name already exists.");
  await expect(rename.getByLabel("Name (required)")).toHaveAttribute("aria-invalid", "true");
  await editor.keyboard.press("Escape");
  const savedFilters = (url: URL) => url.pathname === "/api/v1/saved-filters";
  await editor.route(savedFilters, (route) => (route.request().method() === "POST"
    ? route.fulfill({ status: 400, contentType: "application/problem+json", json: { type: "/problems/validation", title: "Invalid", status: 400, detail: "Fix the fields.", errors: { query: ["owner: A saved view can't hold this filter: the list page doesn't apply it."] } } })
    : route.continue()));
  await editor.getByRole("button", { name: "Save as view…" }).click();
  const save = editor.getByRole("dialog", { name: "Save as view" });
  await save.getByLabel("Name (required)").fill(`Owner filter ${t}`);
  await save.getByRole("button", { name: "Save view" }).click();
  await expect(save.getByRole("alert")).toContainText("owner isn't a filter the list page applies, so a saved view can't keep it. Remove it and save again.");
  await expectNoViolations(editor, "Save as view refused by the query allow-list");
  await editor.keyboard.press("Escape");
  await editor.unroute(savedFilters);

  // Delete: shared, so it goes for everyone; the Admin-made subscription now names a deleted view in the profile.
  await reopened.getByRole("button", { name: "Delete view…" }).click();
  const confirm = editor.getByRole("dialog", { name: "Delete saved view?" });
  await expect(confirm).toContainText("is shared, so it disappears for everyone");
  await expectNoViolations(editor, "Delete saved view dialog");
  await confirm.getByRole("button", { name: `Delete “${renamed}”` }).click();
  await said(editor, `Deleted saved view “${renamed}”.`);
  await expect(editor.getByLabel("Saved views", { exact: true })).toBeFocused();
  await expect(editor.getByLabel("Saved views", { exact: true }).locator("option", { hasText: renamed })).toHaveCount(0);
  await editor.goto("/profile");
  const mine = editor.getByRole("table", { name: "Your weekly saved view emails" });
  await expect(mine.getByRole("row").filter({ hasText: "View deleted" })).toContainText("Tuesday");
  await mine.getByRole("button", { name: "Unsubscribe from the deleted view" }).first().click();
  await said(editor, "Unsubscribed from the deleted view.");
});

test("A Viewer's limits: private views only, can't change a shared view, renames and deletes their own", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  const t = token();
  await page.goto(`/applications?q=${t}&preset=all`);
  await page.getByRole("button", { name: "Save as view…" }).click();
  const dialog = page.getByRole("dialog", { name: "Save as view" });
  await expect(dialog.getByLabel("Share with everyone")).toHaveCount(0);
  await expect(dialog).toContainText("Only you see this view. Editors and Admins can share views.");
  await dialog.getByLabel("Name (required)").fill(`Mine ${t}`);
  await dialog.getByRole("button", { name: "Save view" }).click();
  await said(page, `Saved view “Mine ${t}”.`);
  const open = page.getByRole("region", { name: `View: Mine ${t}` });
  await expect(open).toContainText("Only you");
  // The API refuses sharing for a Viewer as well.
  const id = viewIdOf(page);
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  // With If-Match (the e2e server requires it from a session), so the refusal checked is the sharing rule.
  expect((await page.request.patch(`/api/v1/saved-filters/${id}`, { data: { shared: true }, headers: { "X-CSRFToken": csrf, "If-Match": "*" } })).status()).toBe(403);

  await open.getByRole("button", { name: "Rename or share…" }).click();
  const edit = page.getByRole("dialog", { name: `Rename or share “Mine ${t}”` });
  await expect(edit.getByLabel("Share with everyone")).toHaveCount(0);
  await edit.getByLabel("Name (required)").fill(`Mine renamed ${t}`);
  await edit.getByRole("button", { name: "Save" }).click();
  await said(page, `Saved “Mine renamed ${t}”, only you can see it.`);
  await expect(page.getByRole("button", { name: "Rename or share…" })).toBeFocused();
  await expectNoViolations(page, "A Viewer's own view");

  // A shared view by an Editor: no Save changes even when the filters differ, no rename or delete.
  const editor = await as(browser, baseURL, EDITOR);
  const shared: View = await post(editor, "saved-filters", { name: `Shared ${t}`, kind: "application", query: { lifecycle: "active" }, shared: true });
  await page.goto(`/applications?view=${shared.id}`);
  const seen = page.getByRole("region", { name: `View: ${shared.name}` });
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Criticality", exact: true }).click();
  await page.getByLabel("Criticality", { exact: true }).selectOption({ label: "High" });
  await expect(seen).toContainText("Changed since saved: filters");
  await expect(seen.getByRole("button", { name: /^Save changes/ })).toHaveCount(0);
  await expect(seen.getByRole("button", { name: "Rename or share…" })).toHaveCount(0);
  await expect(seen.getByRole("button", { name: "Delete view…" })).toHaveCount(0);
  await expect(seen.getByRole("button", { name: "Email me weekly…" })).toBeVisible();
  await expectNoViolations(page, "A shared view a Viewer can't change");

  // Their own view: delete it.
  await page.getByLabel("Saved views", { exact: true }).selectOption({ label: `Mine renamed ${t}` });
  await page.getByRole("region", { name: `View: Mine renamed ${t}` }).getByRole("button", { name: "Delete view…" }).click();
  const confirm = page.getByRole("dialog", { name: "Delete saved view?" });
  await expect(confirm).toContainText("is removed from your saved views");
  await confirm.getByRole("button", { name: `Delete “Mine renamed ${t}”` }).click();
  await said(page, `Deleted saved view “Mine renamed ${t}”.`);
});

test("Filters equal to a stored view's offer \"Open this view\"; a deleted view leaves the selector at once; owner, support team and IT steward chips", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  const t = token();
  const owner = await post(page, "people", { name: `${t} Owner` });
  const team = await post(page, "teams", { name: `${t} Team` });
  const stewards: { id: number; name: string; email: string }[] = await (await page.request.get("/api/v1/stewards")).json();
  const steward = stewards.find((s) => s.email === ADMIN)!;
  const otherSteward = stewards.find((s) => s.email === EDITOR)!;
  await post(page, "applications", { name: `${t} Owned`, lifecycle: "active", technical_owner: owner.id, support_team: team.id, it_steward: steward.id });
  // Another steward: a record created without one may get its creator as IT steward.
  await post(page, "applications", { name: `${t} Other`, lifecycle: "active", it_steward: otherSteward.id });
  const name = `Active ${t}`;
  const view = await post(page, "saved-filters", { name, kind: "application", query: { q: t, lifecycle: "active" }, shared: false }) as { id: string };

  // The same filters set by hand, without view=: the selector shows the view, which "Open this view" opens.
  await page.goto(`/applications?q=${t}&lifecycle=active`);
  const select = page.getByLabel("Saved views", { exact: true });
  await expect(select).toHaveValue(view.id);
  await expect(page.getByTestId("view-matched")).toContainText(`These filters are the same as “${name}”.`);
  await expect(page.getByRole("region", { name: `View: ${name}` })).toHaveCount(0);
  await expectNoViolations(page, "Filters equal to a stored view's");
  await page.getByRole("button", { name: "Open this view" }).click();
  await expect(page).toHaveURL(new RegExp(`view=${view.id}`));
  await expect(page.getByRole("region", { name: `View: ${name}` })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open this view" })).toHaveCount(0);
  await expect(page.getByTestId("view-matched")).toHaveCount(0);

  // Deleted: its name leaves the selector at once, while the listing is still reloading (held here for 4 seconds).
  let hold = false;
  await page.route((url) => url.pathname === "/api/v1/saved-filters" && url.searchParams.get("kind") === "application", async (route) => {
    if (hold && route.request().method() === "GET") await new Promise((done) => setTimeout(done, 4_000));
    await route.continue();
  });
  await page.getByRole("region", { name: `View: ${name}` }).getByRole("button", { name: "Delete view…" }).click();
  const remove = page.getByRole("dialog", { name: "Delete saved view?" });
  hold = true;
  await remove.getByRole("button", { name: `Delete “${name}”` }).click();
  await expect(remove).toHaveCount(0);
  await expect(page).not.toHaveURL(/view=/);
  await expect(select.locator("option", { hasText: name })).toHaveCount(0, { timeout: 2_000 });
  await expect(select).toHaveValue("", { timeout: 2_000 });
  await expect(page.getByTestId("view-matched")).toHaveCount(0);
  hold = false;

  // Owner (business or technical), support team and IT steward are filter chips, in the URL.
  const table = page.getByRole("region", { name: "Applications table" });
  const chipFilter = async (label: string, option: string, param: string, value: string) => {
    await page.goto(`/applications?q=${t}`);
    // The search also ranks near-identical names from other runs, so only these two are checked before the chip.
    await expect.poll(() => shown(page, "Applications")).toEqual(expect.arrayContaining([`${t} Other`, `${t} Owned`]));
    await page.getByRole("button", { name: "Add filter" }).click();
    await page.getByRole("menuitem", { name: label }).click();
    await page.getByLabel(label, { exact: true }).selectOption({ label: option });
    await expect(page).toHaveURL(new RegExp(`${param}=${value}`));
    await expect.poll(() => shown(page, "Applications").then((x) => x.filter((n) => n.startsWith(t)))).toEqual([`${t} Owned`]);
  };
  await chipFilter("Owner (business or technical)", `${t} Owner`, "owner", owner.id);
  await expectNoViolations(page, "The owner filter chip");
  await chipFilter("Support team", `${t} Team`, "support_team", team.id);
  await chipFilter("IT steward", steward.name, "it_steward", String(steward.id));
  await page.reload();
  await expect(page.getByLabel("IT steward", { exact: true })).toHaveValue(String(steward.id));
  await expect(table.getByRole("rowheader", { name: new RegExp(`^${t} `) })).toHaveCount(1);
  await page.context().close();
});
