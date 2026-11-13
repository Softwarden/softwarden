import { expect, test, type Page } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

/** The dependency journeys in the browser. The short check comes first; the full
 *  journey then goes from recording links to retiring with acknowledgment. Steps already covered in depth elsewhere
 *  (relationships.spec.ts, explorer.spec.ts, assessments.spec.ts, retirement.spec.ts) are driven here through the same
 *  screens a user would use, with the data around them made through the API. Synthetic data only. */

test.describe.configure({ mode: "serial" });

const app = (page: Page, name: string, extra: object = {}) => post(page, "applications", { name, lifecycle: "active", ...extra });
const component = (page: Page, name: string, extra: object = {}) => post(page, "components", { name, lifecycle: "active", component_type: "script", ...extra });

/** On a record's Relationships tab: add "this depends on other". */
async function addDependsOn(page: Page, kind: "applications" | "components", id: string, name: string, other: string) {
  await page.goto(`/${kind}/${id}?tab=relationships`);
  await page.getByRole("button", { name: "Add relationship" }).click();
  const dialog = page.getByRole("dialog", { name: `Add relationship — ${name}` });
  await dialog.getByRole("combobox", { name: "Other record (required)" }).fill(other);
  await dialog.getByRole("option", { name: new RegExp(other) }).click();
  await dialog.getByRole("button", { name: "Add relationship" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("region", { name: "Depends on table" }).getByRole("row", { name: new RegExp(other) })).toBeVisible();
}

test("Slice exit check: link three records into a chain with a cycle, see the paths and the cycle, save an assessment with the same dependents", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const core = await app(editor, `${t} Core`);
  const mid = await component(editor, `${t} Mid`);
  const top = await app(editor, `${t} Top`);

  // Mid depends on Core, Top depends on Mid, and Core depends on Top: a cycle.
  await addDependsOn(editor, "components", mid.id, `${t} Mid`, `${t} Core`);
  await addDependsOn(editor, "applications", top.id, `${t} Top`, `${t} Mid`);
  await addDependsOn(editor, "applications", core.id, `${t} Core`, `${t} Top`);

  await editor.goto("/dependencies?view=table");
  const picker = editor.getByRole("combobox", { name: "Target (application or component)" });
  await picker.fill(`${t} Core`);
  await editor.getByRole("option", { name: new RegExp(`${t} Core`) }).click();
  await editor.getByLabel("Depth").selectOption("all");
  const table = editor.getByRole("table", { name: /Dependents of/ });
  await expect(table.getByRole("row", { name: new RegExp(`^${t} Mid`) })).toContainText(`${t} Mid → depends on → ${t} Core`);
  await expect(table.getByRole("row", { name: new RegExp(`^${t} Top`) })).toContainText(`${t} Top → depends on → ${t} Mid → depends on → ${t} Core`);
  await expect(table.getByRole("row", { name: new RegExp(`^${t} Core Closes a loop`) })).toBeVisible(); // the cycle back to the target
  await expect(editor.getByText("A missing path is not evidence that a change is safe.")).toBeVisible();

  // The assessment's snapshot lists the same dependents, and the same cycle.
  await editor.getByRole("link", { name: "Start change assessment" }).click();
  await expect(editor.getByRole("combobox", { name: "Target (required)" })).toHaveValue(`${t} Core`); // the target is filled in before the form is used
  await editor.getByRole("radio", { name: "Change" }).check();
  await editor.getByLabel("Title (required)").fill(`${t} Move Core`);
  await editor.getByRole("button", { name: "Create assessment" }).click();
  await expect(editor).toHaveURL(/\/assessments\/[0-9a-f-]{36}$/);
  await expect(editor.getByRole("list", { name: "Snapshot summary" })).toContainText("2 dependents");
  await expect(editor.getByRole("list", { name: "Snapshot summary" })).toContainText("1 cycle");
  const affected = editor.getByRole("table", { name: /^Affected set for/ });
  await expect(affected.getByRole("row", { name: new RegExp(`${t} Mid`) }).first()).toBeVisible();
  await expect(affected.getByRole("row", { name: new RegExp(`${t} Top`) }).first()).toBeVisible();
  await expect(affected.locator("tr", { hasText: "Cycle back to" })).toContainText(`${t} Core`);
});

test("Dependency journey: record links, explore with a cycle, save an assessment, flag it stale by an owner change, refresh with history kept, retire with acknowledgment", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const dana = await personId(editor, "Dana Ortiz");
  const core = await app(editor, `${t} Core`);
  const sync = await component(editor, `${t} Sync`, { technical_owner: dana }); // a script in the path
  const portal = await app(editor, `${t} Portal`);
  const partner = await app(editor, `${t} Partner`);
  const courier = await component(editor, `${t} Courier`);
  await post(editor, "relationships", { type: "depends_on", source: sync.id, target: core.id });
  await post(editor, "relationships", { type: "depends_on", source: core.id, target: portal.id }); // the cycle: Portal -> Sync -> Core -> Portal
  const flow = await post(editor, "integrations", { name: `${t} Feed`, lifecycle: "active", source: core.id, target: partner.id, protocol: "SFTP" });
  await post(editor, "relationships", { type: "implements", source: courier.id, target: flow.id });

  // 1. Record a link from the screen: Portal depends on Sync.
  await addDependsOn(editor, "applications", portal.id, `${t} Portal`, `${t} Sync`);

  // 2. Explore: the path runs through the component, the cycle is marked and not followed, the data flow is apart.
  await editor.goto(`/dependencies?view=table&target=${core.id}&depth=all`);
  const explorer = editor.getByRole("table", { name: /Dependents of/ });
  await expect(explorer.getByRole("row", { name: new RegExp(`^${t} Portal`) }).first()).toContainText(`${t} Portal → depends on → ${t} Sync → depends on → ${t} Core`);
  await expect(explorer.getByRole("row", { name: new RegExp(`^${t} Core Closes a loop`) })).toBeVisible();
  await expect(explorer.getByRole("rowgroup").filter({ hasText: "data flow, not operational" })).toContainText(`${t} Feed`);

  // 3. Save an assessment from the explorer; the snapshot has the component in the path and the integration's implementer.
  await editor.getByRole("link", { name: "Start change assessment" }).click();
  await expect(editor.getByRole("combobox", { name: "Target (required)" })).toHaveValue(`${t} Core`);
  await editor.getByRole("radio", { name: "Retirement" }).check();
  await editor.getByLabel("Title (required)").fill(`${t} Retire Core`);
  await editor.getByRole("button", { name: "Create assessment" }).click();
  await expect(editor).toHaveURL(/\/assessments\/[0-9a-f-]{36}$/);
  const assessmentUrl = editor.url();
  await expect(editor.getByText(/Assessment created\. Affected set saved: 2 dependents, 1 data flow\./).first()).toBeVisible();
  const affected = editor.getByRole("table", { name: /^Affected set for/ });
  await expect(affected.getByRole("row", { name: new RegExp(`${t} Sync`) }).first()).toContainText("Component");
  await expect(affected.getByRole("row", { name: new RegExp(`${t} Feed`) })).toContainText(`Implemented by ${t} Courier`);
  await expect(editor.getByRole("status").filter({ hasText: "This snapshot is stale" })).toHaveCount(0);

  // 4. An unrelated edit does not flag it; changing a node's owner does.
  await post(editor, `components/${sync.id}`, { description: "Synthetic description, not a trigger" }, "PATCH");
  await editor.reload();
  await expect(editor.getByRole("status").filter({ hasText: "This snapshot is stale" })).toHaveCount(0);
  await editor.goto(`/components/${sync.id}`);
  await editor.getByRole("button", { name: "Edit ownership" }).click();
  const owners = editor.getByRole("dialog", { name: `Assign owners — ${t} Sync` });
  await owners.getByRole("combobox", { name: "Technical owner" }).fill("Priya Raman");
  await editor.getByRole("option", { name: /^Priya Raman/ }).first().click();
  await owners.getByRole("button", { name: "Save" }).click();
  await expect(editor.getByRole("status").filter({ hasText: "Technical owner set to Priya Raman." })).toBeVisible();

  await editor.goto(assessmentUrl);
  const banner = editor.getByRole("status").filter({ hasText: "This snapshot is stale" });
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(`${t} Sync technical owner changed`);
  await expect(editor.getByRole("table", { name: /^Affected set for/ }).getByRole("row", { name: new RegExp(`${t} Sync`) }).first()).toContainText("Owners changed since the snapshot");
  // The staleness is in Needs attention as well, routed to the assessment's owner.
  await editor.goto("/?scope=all");
  await expect(editor.getByRole("table", { name: /Stale assessments/ }).getByRole("row", { name: new RegExp(`${t} Retire Core`) })).toBeVisible();

  // 5. Refresh: a new current snapshot, the stale one kept in history.
  await editor.goto(assessmentUrl);
  await editor.getByRole("button", { name: "Refresh snapshot" }).click();
  await expect(banner).toHaveCount(0);
  await editor.getByRole("button", { name: "Snapshot history (2)" }).click();
  const history = editor.getByRole("table", { name: /^Snapshot history/ });
  await expect(history.getByRole("row")).toHaveCount(3);
  await expect(history.getByText("Stale snapshot")).toBeVisible(); // the earlier one stays stale
  await history.getByRole("button", { name: /^View snapshot taken/ }).click();
  await expect(editor.getByText(/You are viewing the snapshot taken/)).toBeVisible();
  await editor.getByRole("button", { name: "Back to the current snapshot" }).click();

  // 6. Retire Core, which Sync depends on: refused until acknowledged with a reason; the links stay.
  await editor.goto(`/applications/${core.id}/edit`);
  await editor.getByLabel("Lifecycle (required)").selectOption("retired");
  await editor.getByRole("button", { name: "Save" }).click();
  const dialog = editor.getByRole("dialog", { name: `Retire ${t} Core?` });
  await expect(dialog.getByRole("list", { name: "Active dependents" })).toContainText(`${t} Sync`);
  await dialog.getByLabel(/^Reason \(required/).fill(`${t} replaced by the vendor platform`);
  await dialog.getByRole("checkbox", { name: "I acknowledge 1 active dependent" }).check();
  await dialog.getByRole("button", { name: "Retire with acknowledgment" }).click();
  await expect(editor.getByText(new RegExp(`Retired with acknowledgment of 1 active dependent\\. Reason saved to the audit history: ${t} replaced by the vendor platform`)).first()).toBeVisible();
  await editor.getByRole("tab", { name: /^Relationships/ }).click();
  await expect(editor.getByRole("region", { name: "Is needed by table" }).getByRole("link", { name: `${t} Sync` })).toBeVisible(); // the link is kept
  await editor.getByRole("tab", { name: "Audit activity" }).click();
  await expect(editor.getByRole("region", { name: "Audit activity table" })).toContainText(`Reason: ${t} replaced by the vendor platform`);
});
