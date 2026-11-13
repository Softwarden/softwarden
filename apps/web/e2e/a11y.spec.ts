import { expectNoViolations } from "./axe";
import { pageAs, personId, post, token, showAllClear } from "./helpers";
import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

/** Axe on every core screen (WCAG 2.2 AA tags), as Admin so every control is present, plus dialogs and the sign-in page. */

const PASSWORD = "softwarden-demo-2026"; // synthetic demo accounts from seed_demo
// axe runs in the light and the dark theme at every check (e2e/axe.ts), so these tests get three minutes.
test.describe.configure({ mode: "serial", timeout: 180_000 });

let state: Awaited<ReturnType<BrowserContext["storageState"]>> | null = null;
async function adminPage(browser: Browser, baseURL?: string): Promise<Page> {
  if (!state) {
    const page = await browser.newPage({ baseURL });
    await page.goto("/login");
    await page.getByLabel("Email").fill("admin@example.test");
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible(); // everyone's home
    state = await page.context().storageState();
    await page.close();
  }
  return (await browser.newContext({ baseURL, storageState: state })).newPage();
}

test("Sign-in page", async ({ page }) => {
  await page.goto("/login");
  await expectNoViolations(page, "Sign-in");
});

const SCREENS: [string, string, string][] = [
  ["Needs attention", "/", "Needs attention"],
  ["Applications list", "/applications", "Applications"],
  ["Components list", "/applications?kind=components", "Components"],
  ["Add application", "/applications/new", "Add application"],
  ["Add component", "/applications/new?kind=component", "Add component"],
  ["Integrations list", "/integrations", "Integrations"],
  ["Add integration", "/integrations/new", "Add integration"],
  ["Change assessments", "/assessments", "Change assessments"],
  ["New assessment", "/assessments/new", "New assessment"],
  ["People", "/admin/people", "People & Teams"],
  ["Teams", "/admin/people?tab=teams", "People & Teams"],
  ["Departments", "/admin/people?tab=departments", "People & Teams"],
  ["Vendors", "/vendors", "Vendors"],
  ["Users & roles", "/admin/users", "Users & roles"],
  ["Organization settings", "/admin/organization", "Organization settings"],
  ["Audit log", "/admin/audit", "Audit log"],
  ["Custom fields", "/custom-fields", "Custom fields"],
  ["Relationship types", "/relationship-types", "Relationship types"],
];

for (const [what, path, heading] of SCREENS) {
  test(what, async ({ browser, baseURL }) => {
    const page = await adminPage(browser, baseURL);
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    await page.waitForLoadState("networkidle");
    await expectNoViolations(page, what);
  });
}

test("Record detail tabs and dialogs", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  const t = token(); // the suite starts on an empty database, so make the records this test needs
  const app = await post(page, "applications", { name: `${t} A11y`, lifecycle: "active" });
  await post(page, `applications/${app.id}/instances`, { name: `${t} Production`, environment: "production" });
  const other = await post(page, "components", { name: `${t} Other`, lifecycle: "active", component_type: "script" });
  await post(page, "relationships", { type: "depends_on", source: app.id, target: other.id, purpose: "Nightly" });
  await post(page, "relationships", { type: "depends_on", source: other.id, target: app.id });
  await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: app.id, target: other.id });
  await page.goto(`/applications/${app.id}`);
  await expect(page.getByRole("tab", { name: "Overview & ownership" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Application detail");
  for (const tab of [/^Instances/, /^Relationships/, /^Review history/, "Audit activity"]) {
    await page.getByRole("tab", { name: tab }).click();
    await page.waitForLoadState("networkidle");
    await expectNoViolations(page, `Detail tab ${tab}`);
  }
  await page.getByRole("tab", { name: /^Relationships/ }).click();
  await page.getByRole("button", { name: "Add relationship" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("combobox", { name: "Other record (required)" }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Add relationship dialog");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /^Confirm link with/ }).first().click();
  await expect(page.getByRole("dialog", { name: "Confirm link" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Confirm link dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /^History of confirmations/ }).first().click();
  await expect(page.getByRole("dialog", { name: "Confirmation history" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Confirmation history dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Overview & ownership" }).click();
  await page.getByRole("button", { name: "Edit ownership" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Assign owners dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Record review" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Record review dialog");
});

test("Integration detail, its tabs and dialogs", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  const t = token();
  const source = await post(page, "applications", { name: `${t} Source`, lifecycle: "active" });
  const target = await post(page, "components", { name: `${t} Target`, lifecycle: "active", component_type: "script" });
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: source.id, target: target.id, protocol: "SFTP" });
  await post(page, "relationships", { type: "implements", source: target.id, target: flow.id });
  await page.goto("/integrations?q=" + t);
  await expect(page.getByRole("link", { name: `${t} Flow` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Integrations list with rows");
  await page.goto(`/integrations/${flow.id}`);
  await expect(page.getByRole("heading", { name: `${t} Flow`, level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: `${t} Target`, exact: true }).first()).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Integration detail");
  await page.getByRole("button", { name: "Add implementer" }).click();
  await page.getByRole("combobox", { name: "Component (required)" }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await expectNoViolations(page, "Add implementer dialog");
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Edit ownership" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Integration owners dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Confirm link", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Confirm link" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Integration confirm link dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: /^Confirmation history/ }).click();
  await expectNoViolations(page, "Integration confirmation history");
  await page.getByRole("tab", { name: "Audit activity" }).click();
  await expect(page.getByRole("region", { name: "Audit activity table" })).toBeVisible();
  await expectNoViolations(page, "Integration audit activity");
  await page.goto(`/integrations/${flow.id}/edit`);
  await expect(page.getByLabel("Name (required)")).toHaveValue(`${t} Flow`);
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Edit integration");
});

test("Needs attention with Links to verify and Stale assessments", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  const t = token();
  const portal = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const sync = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  const link = await post(page, "relationships", { type: "depends_on", source: portal.id, target: sync.id });
  await post(page, `relationships/${link.id}/confirmations`, { confirmed_by: await personId(page, "Dana Ortiz"), evidence: "Email" });
  await post(page, `applications/${portal.id}`, { technical_owner: await personId(page, "Priya Raman") }, "PATCH"); // the link needs verification
  const assessment = await post(page, "assessments", { target: sync.id, change_type: "retirement", title: `${t} Retire Sync` });
  await post(page, "relationships", { type: "depends_on", source: (await post(page, "applications", { name: `${t} New`, lifecycle: "active" })).id, target: sync.id }); // stales it
  for (const scope of ["all", "unassigned"]) {
    await page.goto(`/?scope=${scope}`);
    await showAllClear(page); // an empty queue is on the All clear line
    await expect(page.getByRole("heading", { name: "Links to verify" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Stale assessments" })).toBeVisible();
    await page.waitForLoadState("networkidle");
    await expectNoViolations(page, `Needs attention, ${scope}`);
  }
  await page.goto(`/assessments/${assessment.id}`);
  await expect(page.getByRole("heading", { name: `${t} Retire Sync`, level: 1 })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Assessment detail from Needs attention");
});

test("Dependencies: no target, Table with the side panel, Graph, no relationships and the confirm dialog", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  const t = token();
  const target = await post(page, "applications", { name: `${t} Target`, lifecycle: "active" });
  const sync = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  const portal = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const other = await post(page, "applications", { name: `${t} Other`, lifecycle: "active" });
  const link = await post(page, "relationships", { type: "depends_on", source: sync.id, target: target.id });
  await post(page, "relationships", { type: "depends_on", source: portal.id, target: sync.id });
  await post(page, "relationships", { type: "depends_on", source: target.id, target: portal.id }); // a cycle
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: target.id, target: other.id });
  await post(page, "relationships", { type: "implements", source: sync.id, target: flow.id });
  await post(page, `relationships/${link.id}/confirmations`, { confirmed_by: await personId(page, "Dana Ortiz"), evidence: "Email" });
  await post(page, `components/${sync.id}`, { technical_owner: await personId(page, "Priya Raman") }, "PATCH"); // Needs verification

  await page.goto("/dependencies");
  await expect(page.getByText("Choose an application or component to see what depends on it.")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Dependencies, no target");

  const query = `target=${target.id}&direction=both&depth=all`;
  await page.goto(`/dependencies?${query}&view=graph`);
  await expect(page.getByRole("application", { name: `Dependency graph for ${t} Target` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Dependencies, Graph");

  await page.goto(`/dependencies?${query}&view=table`);
  await page.getByRole("button", { name: `Show path for ${t} Sync` }).first().click();
  await expect(page.getByRole("heading", { name: `Selected: ${t} Sync` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Dependencies, Table with the side panel");
  await page.getByRole("button", { name: /^Confirm link/ }).click();
  await expect(page.getByRole("dialog", { name: "Confirm link" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Dependencies, Confirm link dialog");
  await page.keyboard.press("Escape");

  const lonely = await post(page, "applications", { name: `${t} Lonely`, lifecycle: "active" });
  await page.goto(`/dependencies?target=${lonely.id}&view=table`);
  await expect(page.getByText(`Nothing is recorded as depending on ${t} Lonely.`)).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Dependencies, no relationships");
});

test("Keyboard: skip link, sidebar and tab order reach the main content", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  await page.goto("/applications");
  await expect(page.getByRole("heading", { name: "Applications", level: 1 })).toBeVisible();
  // From the top of the page, the first Tab stop is the skip link.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("Tab");
  const first = await page.evaluate(() => document.activeElement?.textContent ?? "");
  expect(first.toLowerCase()).toContain("skip");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  const inMain = await page.evaluate(() => !!document.activeElement?.closest("main"));
  expect(inMain).toBe(true);
});

test("Change assessments: list with rows, New assessment with errors, detail (stale, history, table), menu and dialogs", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  const t = token();
  const core = await post(page, "applications", { name: `${t} Core`, lifecycle: "active" });
  const mid = await post(page, "applications", { name: `${t} Mid`, lifecycle: "active", technical_owner: await personId(page, "Priya Raman") });
  const top = await post(page, "components", { name: `${t} Top`, lifecycle: "active", component_type: "script" });
  const old = await post(page, "applications", { name: `${t} Old`, lifecycle: "retired" });
  const other = await post(page, "applications", { name: `${t} Partner`, lifecycle: "active" });
  for (const [source, target] of [[mid, core], [top, mid], [core, top], [old, core]]) await post(page, "relationships", { type: "depends_on", source: source.id, target: target.id });
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: core.id, target: other.id });
  const impl = await post(page, "components", { name: `${t} Courier`, lifecycle: "active", component_type: "script" });
  await post(page, "relationships", { type: "implements", source: impl.id, target: flow.id });
  const retire = await post(page, "assessments", { target: core.id, change_type: "retirement", title: `${t} Retire Core`, description: "Why", findings: "What" });
  await post(page, `applications/${mid.id}`, { lifecycle: "deprecated" }, "PATCH"); // makes the first snapshot stale
  await post(page, `assessments/${retire.id}/refresh`, {}); // a second snapshot, so the history has two rows
  await post(page, `applications/${mid.id}`, { lifecycle: "active" }, "PATCH");

  await page.goto(`/assessments?q=${t}`);
  await expect(page.getByRole("link", { name: `${t} Retire Core` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Assessments list with rows and the Stale flag");

  await page.goto(`/assessments/new?target=${core.id}`);
  await expect(page.getByRole("combobox", { name: "Target (required)" })).toHaveValue(`${t} Core`);
  await page.getByRole("button", { name: "Create assessment" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Fix these fields" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "New assessment with errors");
  await page.getByRole("combobox", { name: "Target (required)" }).click();
  await expect(page.getByRole("listbox")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "New assessment with the target list open");
  await page.keyboard.press("Escape");

  await page.goto(`/assessments/${retire.id}`);
  await expect(page.getByRole("heading", { name: `${t} Retire Core`, level: 1 })).toBeVisible();
  await expect(page.getByRole("table", { name: /^Affected set for/ })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Assessment detail with a fresh snapshot");
  await page.getByRole("button", { name: /^Snapshot history/ }).click();
  await page.getByRole("button", { name: /^View snapshot taken/ }).click();
  await expect(page.getByText(/You are viewing the snapshot taken/)).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Assessment detail, earlier snapshot with history open");
  await page.getByRole("button", { name: "Back to the current snapshot" }).click();

  await post(page, "relationships", { type: "depends_on", source: (await post(page, "applications", { name: `${t} Newcomer`, lifecycle: "active" })).id, target: core.id });
  await page.reload();
  await expect(page.getByRole("status").filter({ hasText: "This snapshot is stale" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Assessment detail, stale banner and header primary");

  await page.getByRole("button", { name: /^Change status/ }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await expectNoViolations(page, "Change status menu open");
  await page.getByRole("menuitem", { name: "Ready for external execution" }).click();
  await expect(page.getByRole("dialog", { name: "Change status to Ready for external execution?" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Change status confirm dialog");
  await page.getByRole("dialog").getByRole("button", { name: "Change status" }).click();
  await expect(page.getByRole("list", { name: "Active dependents" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Retirement acknowledgment dialog from the status menu");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Edit details" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Edit assessment details dialog");
  await page.keyboard.press("Escape");

  await page.goto(`/applications/${core.id}/edit`);
  await page.getByLabel("Lifecycle (required)").selectOption("retired");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("dialog", { name: `Retire ${t} Core?` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Retire with acknowledgment dialog from the edit form");
});

test("Assessment detail as a Viewer, and the Dependencies header with Start change assessment", async ({ browser, baseURL }) => {
  const admin = await adminPage(browser, baseURL);
  const t = token();
  const core = await post(admin, "applications", { name: `${t} Core`, lifecycle: "active" });
  const a = await post(admin, "assessments", { target: core.id, change_type: "change", title: `${t} Change`, findings: "Read me" });
  await post(admin, "relationships", { type: "depends_on", source: (await post(admin, "applications", { name: `${t} Late`, lifecycle: "active" })).id, target: core.id });
  await admin.goto(`/dependencies?target=${core.id}`);
  await expect(admin.getByRole("link", { name: "Start change assessment" })).toBeVisible();
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "Dependencies with Start change assessment");
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/assessments/${a.id}`);
  await expect(viewer.getByRole("heading", { name: `${t} Change`, level: 1 })).toBeVisible();
  await expect(viewer.getByText("Read me")).toBeVisible();
  await viewer.waitForLoadState("networkidle");
  await expectNoViolations(viewer, "Assessment detail as a Viewer");
});

// The dependency dialogs and states the tests above don't open (edit and remove a link, remove an implementer, the integration
// archive, delete and refused-delete dialogs, Discard changes on two forms) and the Admin audit log with dependency events.
test("Remaining dependency dialogs: edit and remove a link, integration archive and delete, Discard changes", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  const t = token();
  const app = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const comp = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  const link = await post(page, "relationships", { type: "depends_on", source: app.id, target: comp.id, purpose: "Nightly" });
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: app.id, target: comp.id });
  await post(page, "relationships", { type: "implements", source: comp.id, target: flow.id });
  const dana = await personId(page, "Dana Ortiz");
  await post(page, `relationships/${link.id}/confirmations`, { confirmed_by: dana, evidence: "Email" });

  await page.goto(`/applications/${app.id}?tab=relationships`);
  await page.getByRole("button", { name: `Edit link with ${t} Sync` }).click();
  await expect(page.getByRole("dialog", { name: `Edit relationship — ${t} Portal` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Edit relationship dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: `Remove link with ${t} Sync` }).click();
  await expect(page.getByRole("dialog", { name: "Remove this relationship?" })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Remove relationship dialog");
  await page.keyboard.press("Escape");

  await page.goto(`/integrations/${flow.id}`);
  await page.getByRole("button", { name: `Remove implementer ${t} Sync` }).click();
  await expect(page.getByRole("dialog", { name: "Remove this relationship?" })).toBeVisible();
  await expectNoViolations(page, "Remove implementer dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Archive" }).click();
  await expect(page.getByRole("dialog", { name: `Archive ${t} Flow?` })).toBeVisible();
  await expectNoViolations(page, "Archive integration dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const del = page.getByRole("dialog", { name: `Delete ${t} Flow?` });
  await del.getByLabel("Name (required)").fill(`${t} Flow`);
  await del.getByRole("button", { name: `Delete ${t} Flow` }).click();
  await expect(del.getByRole("alert")).toContainText("can't be deleted because it has 1 relationship");
  await expectNoViolations(page, "Refused integration delete dialog");
  await page.keyboard.press("Escape");

  await page.goto(`/integrations/${flow.id}/edit`);
  await page.getByLabel("Name (required)").fill(`${t} Renamed`);
  await page.getByRole("link", { name: "Cancel" }).click();
  await expect(page.getByRole("dialog", { name: "Discard changes?" })).toBeVisible();
  await expectNoViolations(page, "Discard changes dialog on the integration form");
  await page.getByRole("button", { name: "Keep editing" }).click();

  await page.goto("/assessments/new");
  await page.getByLabel("Title (required)").fill("Unsaved");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications" }).click();
  await expect(page.getByRole("dialog", { name: "Discard changes?" })).toBeVisible();
  await expectNoViolations(page, "Discard changes dialog on the assessment form");
});

test("Admin audit log with link, confirmation and assessment events", async ({ browser, baseURL }) => {
  const page = await adminPage(browser, baseURL);
  const t = token();
  const app = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const comp = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  const link = await post(page, "relationships", { type: "depends_on", source: app.id, target: comp.id });
  await post(page, `relationships/${link.id}/confirmations`, { confirmed_by: await personId(page, "Dana Ortiz"), evidence: "Email" });
  await post(page, "assessments", { target: comp.id, change_type: "change", title: `${t} Change Sync` });
  await page.goto("/admin/audit");
  const log = page.getByRole("region", { name: "Audit log table" });
  await expect(log.getByRole("link", { name: `${t} Portal depends on ${t} Sync` }).first()).toBeVisible();
  await expect(log.getByRole("link", { name: `${t} Change Sync` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Admin audit log with dependency events");
  await page.getByLabel("Event").selectOption({ label: "Link confirmations" });
  await expect(log.getByRole("row", { name: /Link confirmed/ }).first()).toBeVisible();
  await expectNoViolations(page, "Admin audit log filtered to link confirmations");
});
