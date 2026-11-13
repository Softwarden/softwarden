import { expect, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, personId, post, token } from "./helpers";

/** AI agents (list, filters, detail, form), the agent_gaps Needs attention queue, and the kind-path helper
 *  (an agent opens its own page from Needs attention, the audit log, the explorer and the palette). */

type Model = { id: string; name: string; version: string };

/** A provider and two models made through the API; names carry the test's token so parallel runs don't clash. */
async function models(page: Page, t: string) {
  const provider = await post(page, "ai-providers", { name: `${t} Labs` });
  const alpha: Model = await post(page, "ai-models", { provider: provider.id, name: `${t} Alpha`, version: "1", modality: "text" });
  const beta: Model = await post(page, "ai-models", { provider: provider.id, name: `${t} Beta`, version: "2", modality: "multimodal" });
  return { provider, alpha, beta, label: (m: Model) => `${t} Labs ${m.name} ${m.version}` };
}

const diffRow = (diff: ReturnType<Page["getByRole"]>, label: string) => diff.getByRole("row", { name: new RegExp(`^${label}`) });

test("AI agent: create a Copilot Studio agent with two models, channels and owners, edit it, see the change in History", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const m = await models(page, t);

  await page.goto("/ai-agents");
  await expect(page.getByRole("heading", { level: 1, name: "AI agents" })).toBeVisible();
  await page.getByRole("link", { name: "Add AI agent" }).first().click();
  await expect(page).toHaveURL(/\/ai-agents\/new$/);
  await expect(page.getByRole("heading", { level: 1, name: "Add AI agent" })).toBeVisible();
  await expect(page.getByRole("form", { name: "Add AI agent" })).toBeVisible();
  // The kind is fixed: no application/component switch.
  await expect(page.getByRole("radio", { name: "Application" })).toHaveCount(0);

  await page.getByLabel("Name (required)").fill(`${t} HR Helper`);
  await page.getByLabel("Platform", { exact: true }).selectOption({ label: "Copilot Studio" });
  const modelsGroup = page.getByRole("group", { name: "Models" });
  await modelsGroup.getByRole("checkbox", { name: new RegExp(m.label(m.alpha)) }).check();
  await modelsGroup.getByRole("checkbox", { name: new RegExp(m.label(m.beta)) }).check();
  await expect(modelsGroup).toContainText(`Chosen: ${m.label(m.alpha)}, ${m.label(m.beta)}`);
  const channels = page.getByRole("group", { name: "Channels" });
  await channels.getByRole("checkbox", { name: "Teams" }).check();
  await channels.getByRole("checkbox", { name: "Microsoft 365 Copilot" }).check();
  await page.getByLabel("Data access scope").fill("HR SharePoint site, read only");
  await page.getByLabel("Human in the loop").selectOption({ label: "Yes, a person approves" });
  await page.getByLabel("Actions can write").selectOption({ label: "No, read only" });
  await page.getByLabel("Risk classification").selectOption({ label: "Limited" });
  await page.getByRole("combobox", { name: "Business owner" }).fill("Priya Raman");
  await page.getByRole("option", { name: /^Priya Raman/ }).first().click();
  await page.getByRole("combobox", { name: "Technical owner" }).fill("Dana Ortiz");
  await page.getByRole("option", { name: /^Dana Ortiz/ }).first().click();
  await expectNoViolations(page, "AI agent form, filled");
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(/\/ai-agents\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  await expect(page.getByRole("heading", { level: 1, name: `${t} HR Helper` })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "AI agents" })).toHaveAttribute("href", "/ai-agents");
  const agent = page.getByRole("region", { name: "Agent" });
  await expect(agent).toContainText("Copilot Studio");
  await expect(agent.getByRole("listitem").filter({ hasText: m.label(m.alpha) })).toBeVisible();
  await expect(agent.getByRole("listitem").filter({ hasText: m.label(m.beta) })).toBeVisible();
  await expect(agent.getByRole("list", { name: "Channels" })).toHaveText(/Teams\s*Microsoft 365 Copilot/);
  await expect(agent).toContainText("Limited");
  const governance = page.getByRole("region", { name: "Governance" });
  await expect(governance).toContainText("HR SharePoint site, read only");
  await expect(governance).toContainText("Yes, a person approves");
  await expect(governance).toContainText("No, read only");
  await expect(page.getByRole("region", { name: "Ownership (confirmed fields)" })).toContainText("Priya Raman");
  // With a scope and a decision recorded the agent's own gaps are gone; the shared ones (support team, criticality) stay.
  const status = page.getByRole("group", { name: "Record status" });
  await expect(status).toContainText("No support team");
  await expect(status).not.toContainText("No data access scope");
  await expect(status).not.toContainText("No human-in-loop decision");
  await expectNoViolations(page, "AI agent detail");

  // Edit: If-Match is sent with the ETag read by the form.
  await page.getByRole("link", { name: `Edit ${t} HR Helper` }).click();
  await expect(page).toHaveURL(`/ai-agents/${id}/edit`);
  await expect(page.getByLabel("Risk classification")).toHaveValue("limited");
  await expect(page.getByRole("group", { name: "Models" }).getByRole("checkbox", { name: new RegExp(m.label(m.alpha)) })).toBeChecked();
  await page.getByLabel("Risk classification").selectOption({ label: "High" });
  await page.getByRole("group", { name: "Models" }).getByRole("checkbox", { name: new RegExp(m.label(m.beta)) }).uncheck();
  const [patch] = await Promise.all([
    page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/api/v1/ai-agents/${id}`)),
    page.getByRole("button", { name: "Save" }).click(),
  ]);
  expect(patch.headers()["if-match"]).toMatch(/^"/);
  await expect(page).toHaveURL(`/ai-agents/${id}`);
  await expect(page.getByRole("region", { name: "Agent" })).toContainText("High");
  await expect(page.getByRole("region", { name: "Agent" }).getByRole("listitem").filter({ hasText: m.label(m.beta) })).toHaveCount(0);

  // History shows the edit with its diff.
  await page.getByRole("tab", { name: "Audit activity" }).click();
  const activity = page.getByRole("region", { name: "Audit activity table" });
  const edited = activity.getByRole("row", { name: /Fields changed/ }).first();
  await edited.getByRole("button", { name: "Show changes" }).click();
  const diff = page.getByRole("table", { name: /^Changes in Fields changed/ });
  await expect(diffRow(diff, "Risk classification").locator('[data-side="before"]')).toContainText(/Limited/i);
  await expect(diffRow(diff, "Risk classification").locator('[data-side="after"]')).toContainText(/High/i);
  await expect(diff).toContainText(`${t} Beta`);
  await expectNoViolations(page, "AI agent history with a diff");
});

test("AI agent edit refused when someone else saved first (If-Match, 412)", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const created = await post(page, "ai-agents", { name: `${t} Race`, lifecycle: "active" });
  await page.goto(`/ai-agents/${created.id}/edit`);
  await expect(page.getByLabel("Name (required)")).toHaveValue(`${t} Race`);
  // Someone else changes it meanwhile.
  await post(page, `ai-agents/${created.id}`, { purpose: "Changed elsewhere" }, "PATCH");
  await page.getByLabel("Purpose").fill("Mine");
  await page.getByLabel("Risk classification").selectOption({ label: "High" });
  await page.getByRole("button", { name: "Save" }).click();
  const alert = page.getByRole("alert").filter({ hasText: "Someone else saved changes" });
  await expect(alert).toBeVisible();
  await expect(page).toHaveURL(`/ai-agents/${created.id}/edit`);
  // What was typed is still in the form until the reload…
  await expect(page.getByLabel("Purpose")).toHaveValue("Mine");
  await expectNoViolations(page, "AI agent form after a 412");
  await alert.getByRole("button", { name: "Load the latest version" }).click();
  await expect(page.getByLabel("Purpose")).toHaveValue("Changed elsewhere");
  await expect(page.getByRole("alert").filter({ hasText: "Someone else saved changes" })).toHaveCount(0);
  // …and listed above the reloaded form afterwards.
  const unsaved = page.getByRole("region", { name: "Your unsaved values" });
  await expect(unsaved.getByRole("definition").filter({ hasText: "Mine" })).toBeVisible();
  await expect(unsaved).toContainText(/Purpose\s*Mine/);
  await expect(unsaved).toContainText(/Risk classification\s*High/);
  await expectNoViolations(page, "AI agent form with the unsaved values listed");
  // Saving now works against the latest version.
  await page.getByLabel("Purpose").fill("Mine again");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(`/ai-agents/${created.id}`);

  // The owner dialog sends If-Match too: a change saved meanwhile is refused with a reload prompt.
  await page.getByRole("button", { name: "Edit ownership" }).click();
  const owners = page.getByRole("dialog", { name: `Assign owners — ${t} Race` });
  await expect(owners.getByLabel("Criticality")).toBeVisible();
  await post(page, `ai-agents/${created.id}`, { criticality: "low" }, "PATCH");
  await owners.getByLabel("Criticality").selectOption({ label: "High" });
  await owners.getByRole("button", { name: "Save" }).click();
  const refused = owners.getByRole("alert").filter({ hasText: "Someone else changed" });
  await expect(refused).toBeVisible();
  await refused.getByRole("button", { name: "Load the latest version" }).click();
  await expect(owners.getByLabel("Criticality")).toHaveValue("low");
  await owners.getByLabel("Criticality").selectOption({ label: "High" });
  await owners.getByRole("button", { name: "Save" }).click();
  await expect(owners).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Ownership (confirmed fields)" })).toContainText("High");
});

test("AI agent filters live in the URL; empty, viewer and export states", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const m = await models(page, t);
  const claude = await post(page, "ai-agents", { name: `${t} Claude Desk`, lifecycle: "active", platform: "claude", models_used: [m.alpha.id], channels: ["web"], human_in_loop: true, risk_classification: "high" });
  await post(page, "ai-agents", { name: `${t} Studio Bot`, lifecycle: "active", platform: "copilot_studio", models_used: [m.beta.id], channels: ["teams"] });

  await page.goto(`/ai-agents?q=${t}`);
  const table = page.getByRole("region", { name: "AI agents table" });
  await expect(table.getByRole("link", { name: `${t} Claude Desk` })).toBeVisible();
  await expect(table.getByRole("link", { name: `${t} Studio Bot` })).toBeVisible();
  await expect(table.getByRole("link", { name: `${t} Claude Desk` })).toHaveAttribute("href", `/ai-agents/${claude.id}`);
  // Platform, models and risk are columns by default.
  await expect(table.getByRole("row", { name: new RegExp(`${t} Claude Desk`) })).toContainText(`${t} Labs ${t} Alpha 1`);

  // A chip: Platform = Claude.
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Platform" }).click();
  await page.getByLabel("Platform", { exact: true }).selectOption({ label: "Claude" });
  await expect(page).toHaveURL(/platform=claude/);
  await expect(table.getByRole("link", { name: `${t} Studio Bot` })).toHaveCount(0);
  await expect(table.getByRole("link", { name: `${t} Claude Desk` })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Platform", { exact: true })).toHaveValue("claude");
  await expect(table.getByRole("link", { name: `${t} Studio Bot` })).toHaveCount(0);
  await expectNoViolations(page, "AI agents with a platform filter");

  // Model and human-in-loop from the URL (the models-in-use report links here).
  await page.goto(`/ai-agents?model=${m.beta.id}`);
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue(m.beta.id);
  await expect(table.getByRole("link", { name: `${t} Studio Bot` })).toBeVisible();
  await expect(table.getByRole("link", { name: `${t} Claude Desk` })).toHaveCount(0);
  await page.goto(`/ai-agents?q=${t}&human_in_loop=unknown`);
  await expect(page.getByLabel("Human in the loop", { exact: true })).toHaveValue("unknown");
  await expect(table.getByRole("link", { name: `${t} Studio Bot` })).toBeVisible();
  await expect(table.getByRole("link", { name: `${t} Claude Desk` })).toHaveCount(0);
  await expect(table.getByRole("row", { name: new RegExp(`${t} Studio Bot`) })).toContainText("No human-in-loop decision");
  await page.goto(`/ai-agents?q=${t}&channel=web&provider=${m.provider.id}`);
  await expect(table.getByRole("link", { name: `${t} Claude Desk` })).toBeVisible();
  await expect(table.getByRole("link", { name: `${t} Studio Bot` })).toHaveCount(0);

  // Nothing matches: the filtered empty state with Clear filters.
  await page.goto(`/ai-agents?q=${t}&risk_classification=unacceptable`);
  await expect(page.getByText("No AI agents match these filters")).toBeVisible();
  await expectNoViolations(page, "AI agents, no matches");

  // Export: CSV and Excel from the same dialog, with the list's filters.
  await page.goto(`/ai-agents?q=${t}&platform=claude`);
  await page.getByRole("button", { name: "Export Excel…" }).click();
  const dialog = page.getByRole("dialog", { name: "Export Excel" });
  await expect(dialog).toContainText("Platform: claude");
  const [download] = await Promise.all([page.waitForEvent("download"), dialog.getByRole("button", { name: "Download Excel" }).click()]);
  expect(download.suggestedFilename()).toMatch(/\.xlsx$/);
  await expect(page.getByRole("button", { name: "Export Excel…" })).toBeFocused();

  // A Viewer reads the list: no Add, no import, no checkboxes.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/ai-agents?q=${t}`);
  await expect(viewer.getByRole("region", { name: "AI agents table" }).getByRole("link", { name: `${t} Claude Desk` })).toBeVisible();
  await expect(viewer.getByRole("link", { name: "Add AI agent" })).toHaveCount(0);
  await expect(viewer.getByRole("checkbox", { name: /^Select / })).toHaveCount(0);
  await expectNoViolations(viewer, "AI agents as a Viewer");
});

test("AI agents list: loading, error and first-run empty states", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.route(/\/api\/v1\/ai-agents\?/, (route) => route.fulfill({ status: 500, contentType: "application/problem+json", body: JSON.stringify({ title: "Server error", status: 500, detail: "The server failed." }) }));
  await page.goto("/ai-agents");
  await expect(page.getByRole("alert")).toContainText("The server failed. Nothing was changed.");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await expectNoViolations(page, "AI agents, load failed");
  await page.unroute(/\/api\/v1\/ai-agents\?/);
  await page.route(/\/api\/v1\/ai-agents\?/, (route) => route.fulfill({ json: { results: [], next: null, previous: null } }));
  await page.goto("/ai-agents?preset=active");
  await expect(page.getByText("No AI agents yet")).toBeVisible();
  await expect(page.getByRole("main").getByRole("link", { name: "Add AI agent" })).toHaveCount(2); // header and empty state
  await expectNoViolations(page, "AI agents, none yet");
});

test("Needs attention shows AI agent gaps; each row links to the agent's own page", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const bare = await post(page, "ai-agents", { name: `${t} Ownerless Bot`, lifecycle: "active", criticality: "critical" });
  const dana = await personId(page, "Dana Ortiz");
  const owned = await post(page, "ai-agents", { name: `${t} Scoped Bot`, lifecycle: "active", criticality: "critical", business_owner: dana });

  await page.goto("/?scope=all");
  await expect(page.getByRole("heading", { level: 2, name: /^AI agent gaps/ })).toBeVisible();
  const queue = page.getByRole("region", { name: "Queue table: AI agent gaps" });
  const bareRow = queue.getByRole("row", { name: new RegExp(`${t} Ownerless Bot`) });
  await expect(bareRow).toContainText("No owner");
  await expect(bareRow).toContainText("No data access scope");
  await expect(bareRow).toContainText("No human-in-loop decision");
  await expect(bareRow.getByRole("link", { name: `${t} Ownerless Bot` })).toHaveAttribute("href", `/ai-agents/${bare.id}`);
  const ownedRow = queue.getByRole("row", { name: new RegExp(`${t} Scoped Bot`) });
  await expect(ownedRow).not.toContainText("No owner");
  await expect(ownedRow.getByRole("link", { name: `Fill gaps for ${t} Scoped Bot` })).toHaveAttribute("href", `/ai-agents/${owned.id}/edit`);
  // The visible rows match the count in the queue links and the heading.
  const rows = await queue.locator("tbody tr").count();
  await expect(page.getByRole("navigation", { name: "Queues" }).getByRole("link", { name: /^AI agent gaps/ })).toHaveText(`AI agent gaps ${rows}`);
  await expect(page.getByRole("heading", { name: `AI agent gaps (${rows})` })).toBeVisible();
  // The ownerless agent is also an ownership gap, named as an AI agent and linked to its own page.
  const ownership = page.getByRole("region", { name: "Queue table: Ownership gaps" }).getByRole("row", { name: new RegExp(`${t} Ownerless Bot`) });
  await expect(ownership).toContainText("AI agent");
  await expect(ownership.getByRole("link", { name: `${t} Ownerless Bot` })).toHaveAttribute("href", `/ai-agents/${bare.id}`);
  await expectNoViolations(page, "Needs attention with AI agent gaps");

  // Assign owner opens the agent's own owner dialog (it reads ai-agents/<id>, not components/<id>).
  await bareRow.getByRole("button", { name: `Assign owner for ${t} Ownerless Bot` }).click();
  const dialog = page.getByRole("dialog", { name: `Assign owners — ${t} Ownerless Bot` });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await bareRow.getByRole("link", { name: `${t} Ownerless Bot` }).click();
  await expect(page).toHaveURL(`/ai-agents/${bare.id}`);
  await expect(page.getByRole("heading", { level: 1, name: `${t} Ownerless Bot` })).toBeVisible();
  const status = page.getByRole("group", { name: "Record status" });
  await expect(status).toContainText("No data access scope");
  await expect(status).toContainText("No human-in-loop decision");

  // A Viewer gets View agent instead of the write actions.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/?scope=all");
  const vrow = viewer.getByRole("region", { name: "Queue table: AI agent gaps" }).getByRole("row", { name: new RegExp(`${t} Scoped Bot`) });
  await expect(vrow.getByRole("link", { name: `View agent for ${t} Scoped Bot` })).toHaveAttribute("href", `/ai-agents/${owned.id}`);
});

test("Kind paths: an agent opens its own page from the audit log, the explorer, relationships and the palette", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const app = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  const agent = await post(page, "ai-agents", { name: `${t} Payroll Agent`, lifecycle: "active" });
  await post(page, "relationships", { type: "depends_on", source: agent.id, target: app.id });

  // Explorer: the agent depends on the application, and its row links to /ai-agents/<id>.
  await page.goto(`/dependencies?view=table&target=${app.id}`);
  const table = page.getByRole("table", { name: /Dependents of/ });
  const row = table.getByRole("row", { name: new RegExp(`${t} Payroll Agent`) });
  await expect(row).toContainText("AI agent");
  await expect(row.getByRole("link", { name: `${t} Payroll Agent` }).first()).toHaveAttribute("href", `/ai-agents/${agent.id}`);

  // Relationships tab on the application: the other end is the agent's page.
  await page.goto(`/applications/${app.id}?tab=relationships`);
  await expect(page.getByRole("link", { name: `${t} Payroll Agent` }).first()).toHaveAttribute("href", `/ai-agents/${agent.id}`);

  // Audit log (Admin): the agent's events link to its page.
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto(`/admin/audit?target=${agent.id}`);
  const log = admin.getByRole("region", { name: "Audit log table" });
  await expect(log.getByRole("link", { name: `${t} Payroll Agent` }).first()).toHaveAttribute("href", `/ai-agents/${agent.id}`);

  // The palette finds agents and creates them.
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  const palette = page.getByRole("dialog", { name: "Command palette" });
  const input = palette.getByRole("combobox", { name: "Search records, pages and actions" });
  await page.keyboard.press("Control+k");
  await input.fill(`${t} Payroll Agent`);
  const option = page.getByRole("option", { name: new RegExp(`${t} Payroll Agent.*AI agent`) });
  await expect(option).toBeVisible();
  await expectNoViolations(page, "Palette with an AI agent");
  await option.click();
  await expect(page).toHaveURL(`/ai-agents/${agent.id}`);
  await expect(page.getByRole("heading", { level: 1, name: `${t} Payroll Agent` })).toBeVisible();
  await expect(palette).toHaveCount(0);
  await page.keyboard.press("Control+k");
  await input.fill("Create AI agent");
  await page.getByRole("option", { name: "Create AI agent" }).click();
  await expect(page).toHaveURL(/\/ai-agents\/new$/);
  // g e goes to AI agents; c on the list creates one.
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  await page.keyboard.press("g");
  await page.keyboard.press("e");
  await expect(page.getByRole("heading", { level: 1, name: "AI agents" })).toBeVisible();
  await page.keyboard.press("c");
  await expect(page).toHaveURL(/\/ai-agents\/new$/);
});

test("The models picker's filter box filters on Enter and never submits the form", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const provider = { id: "00000000-0000-4000-8000-000000000001", name: "Mock AI" };
  const models = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota"].map((name, i) => ({
    id: `00000000-0000-4000-8000-0000000001${String(i).padStart(2, "0")}`, name, version: "1", provider, modality: "text", hosting_region: "", usage: 0,
  }));
  await page.route(/\/api\/v1\/ai-models$/, (route) => route.fulfill({ json: models }));
  let posted = false;
  page.on("request", (r) => { if (r.method() === "POST" && r.url().endsWith("/api/v1/ai-agents")) posted = true; });
  await page.goto("/ai-agents/new");
  const group = page.getByRole("group", { name: "Models" });
  const filter = group.getByRole("searchbox", { name: "Filter models" });
  await filter.fill("Gamma");
  await filter.press("Enter");
  await expect(group.getByRole("checkbox")).toHaveCount(1);
  await expect(group.getByRole("checkbox", { name: /Mock AI Gamma 1/ })).toBeVisible();
  await expect(page).toHaveURL(/\/ai-agents\/new$/);
  await expect(page.getByRole("alert").filter({ hasText: "Fix this field" })).toHaveCount(0); // no submit, so no "Enter a name."
  expect(posted).toBe(false);
  await group.getByRole("checkbox", { name: /Mock AI Gamma 1/ }).check();
  await expect(group.getByText("Added Mock AI Gamma 1; 1 chosen.")).toBeAttached();
});
