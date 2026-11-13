import { expect, test, type Page } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

/** The Dependencies screen, Table and Graph views. */

test.describe.configure({ mode: "serial" });

/** Target <- Sync (component) <- Portal, Target <- Kiosk (via Target's Production instance), Target -> Library, and a data
 *  flow Target -> Payroll implemented by Sync. Sync's link needs verification (confirmed, then its owner changed). */
async function scene(page: Page, t: string) {
  const target = await post(page, "applications", { name: `${t} Target`, lifecycle: "active" });
  const production = await post(page, `applications/${target.id}/instances`, { name: `${t} Production`, environment: "production" });
  const sync = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  const portal = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const kiosk = await post(page, "applications", { name: `${t} Kiosk`, lifecycle: "active" });
  const library = await post(page, "components", { name: `${t} Library`, lifecycle: "active", component_type: "script" });
  const payroll = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  const syncLink = await post(page, "relationships", { type: "depends_on", source: sync.id, target: target.id });
  await post(page, "relationships", { type: "depends_on", source: portal.id, target: sync.id });
  const kioskLink = await post(page, "relationships", { type: "depends_on", source: kiosk.id, target: target.id, target_instance: production.id });
  await post(page, "relationships", { type: "depends_on", source: target.id, target: library.id });
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: target.id, target: payroll.id, protocol: "SFTP", schedule: "nightly" });
  await post(page, "relationships", { type: "implements", source: sync.id, target: flow.id });
  const dana = await personId(page, "Dana Ortiz");
  await post(page, `relationships/${syncLink.id}/confirmations`, { confirmed_by: dana, evidence: "Email from Dana" });
  await post(page, `relationships/${kioskLink.id}/confirmations`, { confirmed_by: dana, evidence: "Email from Dana" });
  await post(page, `components/${sync.id}`, { technical_owner: await personId(page, "Priya Raman") }, "PATCH");
  return { target, sync, portal, kiosk, library, payroll, flow, syncLink, kioskLink };
}

const CAUTION = "A missing path is not evidence that a change is safe.";

test("Choose a target, read the Table, widen the depth; data flows sit in their own group; all of it is in the URL", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(page, t);

  await page.goto("/dependencies?view=table");
  await expect(page.getByRole("heading", { name: "Dependencies", level: 1 })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Dependencies" })).toHaveAttribute("aria-current", "page");
  await expect(page.getByText("Choose an application or component to see what depends on it.")).toBeVisible();

  // Target picker: search, choose, and the choice lands in the URL.
  const picker = page.getByRole("combobox", { name: "Target (application or component)" });
  await picker.fill(`${t} Target`);
  await page.getByRole("option", { name: new RegExp(`${t} Target`) }).click();
  await expect(page).toHaveURL(new RegExp(`target=${s.target.id}`));
  await expect(picker).toHaveValue(`${t} Target`);

  // Depth 1, dependents: Sync and Kiosk, with their path, link status and owner.
  const table = page.getByRole("table", { name: /Dependents of .* Target, nearest first/ });
  await expect(table.getByRole("columnheader")).toHaveText(["System", "Kind", "Depth", "Path from target", "Link status", "Owner", "Actions"]);
  const sync = table.getByRole("row", { name: new RegExp(`${t} Sync`) }).first();
  await expect(sync).toContainText("Component");
  await expect(sync).toContainText(`${t} Sync → depends on → ${t} Target`);
  await expect(sync.getByText("Needs verification")).toBeVisible();
  await expect(sync).toContainText("Priya Raman");
  const kiosk = table.getByRole("row", { name: new RegExp(`${t} Kiosk`) });
  await expect(kiosk.getByText(/Confirmed$/)).toBeVisible();
  await expect(kiosk).toContainText("No technical owner");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Portal`) })).toHaveCount(0); // depth 2

  // Data flows: their own tbody with a row-group header, never among the dependents.
  await expect(table.locator("tbody")).toHaveCount(2);
  const group = table.locator('th[scope="rowgroup"]');
  await expect(group).toHaveText("Data flows — data flow, not operational");
  const flow = table.locator("tbody").nth(1).getByRole("row", { name: new RegExp(`${t} Flow`) });
  await expect(flow).toContainText("Integration");
  await expect(flow).toContainText("1 hop");
  await expect(flow).toContainText(`Implemented by ${t} Sync`);
  await expect(flow).toContainText("SFTP nightly");
  await expect(flow.getByText("Unconfirmed")).toBeVisible();

  // Depth is a select (1 to 10 levels, or all); the caution sentence and the verify count are visible; filters are in the URL.
  await page.getByLabel("Depth").selectOption("2");
  await expect(page).toHaveURL(/depth=2/);
  await expect(table.getByRole("row", { name: new RegExp(`${t} Portal`) })).toContainText(`${t} Portal → depends on → ${t} Sync → depends on → ${t} Target`);
  await expect(page.getByText(CAUTION)).toBeVisible();
  await expect(page.getByText("1 link needs verification.", { exact: false })).toBeVisible();

  // Direction: what the target depends on.
  await page.getByRole("group", { name: "Direction" }).getByLabel("Depends on").check();
  await expect(page).toHaveURL(/direction=depends_on/);
  const deps = page.getByRole("table", { name: /Dependencies of .* Target/ });
  await expect(deps.getByRole("row", { name: new RegExp(`${t} Library`) })).toContainText(`${t} Target → depends on → ${t} Library`);
  await page.getByRole("group", { name: "Direction" }).getByLabel("Both").check();
  await expect(page.getByRole("table", { name: /Dependents and dependencies of/ }).getByRole("row", { name: new RegExp(`${t} Library`) })).toBeVisible();

  // Reload keeps everything.
  await page.reload();
  await expect(page.getByRole("group", { name: "Direction" }).getByLabel("Both")).toBeChecked();
  await expect(page.getByLabel("Depth")).toHaveValue("2");
  await expect(picker).toHaveValue(`${t} Target`);
});

test("Toggles: data flows, instances and retired are URL state", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(page, t);
  const old = await post(page, "applications", { name: `${t} Old`, lifecycle: "active" });
  await post(page, "relationships", { type: "depends_on", source: old.id, target: s.target.id });
  await post(page, `applications/${old.id}`, { lifecycle: "retired" }, "PATCH");

  await page.goto(`/dependencies?view=table&target=${s.target.id}`);
  const table = page.getByRole("table", { name: /Dependents of/ });
  await expect(table.getByRole("row", { name: new RegExp(`${t} Kiosk`) })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Old`) })).toHaveCount(0);
  await expect(page.getByText("1 retired left out.")).toBeVisible();

  await page.getByLabel("Include retired").check();
  await expect(page).toHaveURL(/retired=1/);
  const oldRow = table.getByRole("row", { name: new RegExp(`${t} Old`) });
  await expect(oldRow.getByText("Retired", { exact: true })).toBeVisible();

  await page.getByLabel("Show instances").check();
  await expect(page).toHaveURL(/instances=1/);
  await expect(table.getByRole("row", { name: new RegExp(`${t} Kiosk`) })).toContainText(`${t} Kiosk → depends on → ${t} Target (${t} Production)`);

  await page.getByLabel("Show data flows").uncheck();
  await expect(page).toHaveURL(/flows=0/);
  await expect(table.locator("tbody")).toHaveCount(1);
  await expect(page.locator('th[scope="rowgroup"]')).toHaveCount(0);

  await page.getByRole("button", { name: "Reset filters" }).click();
  await expect(page).not.toHaveURL(/retired|instances|flows/);
});

test("Show path fills the side panel (aria-live), Confirm link updates the row and returns focus; Viewers can't confirm", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(page, t);
  await page.goto(`/dependencies?view=table&target=${s.target.id}&depth=2`);

  const panel = page.getByRole("complementary", { name: "Selected record" });
  await expect(panel.locator("[aria-live=polite]")).toBeAttached();
  await expect(panel).toContainText("Choose Show path on a row of the table");

  await page.getByRole("button", { name: `Show path for ${t} Portal` }).click();
  await expect(panel.getByRole("heading", { name: `Selected: ${t} Portal`, level: 2 })).toBeVisible();
  const path = panel.getByRole("list", { name: new RegExp(`Path from .* Target to .* Portal`) });
  await expect(path.getByRole("listitem")).toHaveText([new RegExp(`^${t} Portal$`), new RegExp(`depends on ▸ ${t} Sync$`), new RegExp(`depends on ▸ ${t} Target$`)]);
  await expect(panel).toContainText("Unconfirmed");

  // A different row replaces it.
  await page.getByRole("button", { name: `Show path for ${t} Sync` }).click();
  await expect(panel.getByRole("heading", { name: `Selected: ${t} Sync` })).toBeVisible();
  await expect(panel.getByText("Needs verification")).toBeVisible();
  await expect(panel.getByRole("link", { name: new RegExp(`Open record ${t} Sync`) })).toBeVisible();

  const confirm = panel.getByRole("button", { name: /^Confirm link/ });
  await confirm.click();
  const dialog = page.getByRole("dialog", { name: "Confirm link" });
  await expect(dialog).toContainText(`${t} Sync depends on ${t} Target`);
  await dialog.getByLabel("Confirmed by (required)").selectOption({ label: "Dana Ortiz" });
  await dialog.getByLabel("Evidence / reference (required)").fill("Checked with the owner");
  await dialog.getByRole("button", { name: "Confirm link" }).click();
  await expect(dialog).toBeHidden();
  await expect(confirm).toBeFocused();
  await expect(page.getByText("Link confirmed.").first()).toBeVisible();
  await expect(panel.getByText(/Confirmed$/)).toBeVisible();
  await expect(page.getByText("1 link needs verification.")).toHaveCount(0);

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/dependencies?view=table&target=${s.target.id}`);
  await viewer.getByRole("button", { name: `Show path for ${t} Sync` }).click();
  await expect(viewer.getByRole("complementary", { name: "Selected record" }).getByRole("link", { name: /^Open record/ })).toBeVisible();
  await expect(viewer.getByRole("button", { name: /^Confirm link/ })).toHaveCount(0);
});

test("Cycles are marked and not followed", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const a = await post(page, "applications", { name: `${t} Alpha`, lifecycle: "active" });
  const b = await post(page, "applications", { name: `${t} Beta`, lifecycle: "active" });
  await post(page, "relationships", { type: "depends_on", source: a.id, target: b.id });
  await post(page, "relationships", { type: "depends_on", source: b.id, target: a.id });
  await page.goto(`/dependencies?view=table&target=${a.id}&depth=all`);
  const table = page.getByRole("table", { name: /Dependents of/ });
  await expect(table.getByRole("row", { name: new RegExp(`^${t} Alpha Closes a loop`) })).toBeVisible();
  await page.getByRole("button", { name: `Show path for ${t} Alpha` }).click();
  await expect(page.getByRole("complementary", { name: "Selected record" })).toContainText("this path returns to");
});

test("States: no target, no relationships, error, capped, loading", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const lonely = await post(editor, "applications", { name: `${t} Lonely`, lifecycle: "active" });

  await editor.goto(`/dependencies?view=table&target=${lonely.id}`);
  await expect(editor.getByText(`Nothing is recorded as depending on ${t} Lonely. That doesn't mean nothing does.`)).toBeVisible();
  await expect(editor.getByRole("link", { name: "Add relationship" })).toHaveAttribute("href", `/applications/${lonely.id}?tab=relationships`);
  await expect(editor.getByText(CAUTION)).toBeVisible();
  await editor.getByRole("group", { name: "Direction" }).getByLabel("Depends on").check();
  await expect(editor.getByText(`${t} Lonely has no recorded dependencies. That doesn't mean it has none.`)).toBeVisible();

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/dependencies?view=table&target=${lonely.id}`);
  await expect(viewer.getByText(`Nothing is recorded as depending on ${t} Lonely.`)).toBeVisible();
  await expect(viewer.getByRole("link", { name: "Add relationship" })).toHaveCount(0);

  // Error with Retry, then loading while a slow response is pending.
  let failing = true;
  await editor.route("**/api/v1/dependencies*", async (route) => {
    if (failing) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Boom." }) });
    await new Promise((r) => setTimeout(r, 700));
    return route.continue();
  });
  await editor.goto(`/dependencies?view=table&target=${lonely.id}`);
  await expect(editor.getByRole("alert")).toContainText("Couldn't load dependencies. Boom.");
  failing = false;
  await editor.getByRole("button", { name: "Retry" }).click();
  await expect(editor.getByText("Loading dependencies…")).toBeVisible();
  await expect(editor.getByText(`Nothing is recorded as depending on ${t} Lonely.`)).toBeVisible();
  await editor.unroute("**/api/v1/dependencies*");

  // Capped: 150 of 212 rows.
  await editor.route("**/api/v1/dependencies*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      target: { id: lonely.id, name: `${t} Lonely`, kind: "application", lifecycle: "active", archived: false },
      direction: "dependents", depth: "all", include_retired: false, show_instances: false,
      nodes: [{ id: "11111111-1111-4111-8111-111111111111", kind: "application", name: "Capped row", lifecycle: "active", archived: false, direction: "dependents", depth: 1, cycle: false, link: { relationship: "r1", state: "unconfirmed", why: "Never confirmed.", last_confirmed_on: null },
        path: [{ id: lonely.id, name: `${t} Lonely`, kind: "application", via: null }, { id: "11111111-1111-4111-8111-111111111111", name: "Capped row", kind: "application", via: { relationship: "r1", type: "depends_on" } }], technical_owner: null, gaps: ["No technical owner"] }],
      data_flows: [], total: 212, shown: 150, capped: true,
      summary: { nodes: 211, data_flows: 1, needs_verification: 0, unconfirmed: 1, cycles: 0, hidden_retired: 0 },
    }),
  }));
  await editor.goto(`/dependencies?view=table&target=${lonely.id}&depth=all`);
  await expect(editor.getByText("Showing 150 of 212 rows.")).toBeVisible();
  await editor.getByRole("tab", { name: "Graph" }).click();
  await expect(editor.getByText("Showing 150 of 212 rows. Use the table or reduce depth.")).toBeVisible();
});

test("Graph: a labeled Cytoscape picture with a description, line patterns, a ? mark, a printed SVG copy, and Table is the default when narrow or reduced motion", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(page, t);

  await page.goto(`/dependencies?target=${s.target.id}&depth=2&direction=both`);
  await expect(page.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true"); // wide screen, normal motion
  const graph = page.getByRole("application", { name: `Dependency graph for ${t} Target` });
  await expect(graph).toHaveAttribute("aria-roledescription", "graph");
  await expect(graph).toBeVisible();
  const description = await page.locator(`#${(await graph.getAttribute("aria-describedby"))!.split(" ")[0]}`).textContent();
  expect(description).toContain(`${t} Target is the target`);
  expect(description).toContain(`${t} Sync`);
  expect(description).toContain(`${t} Portal`);
  expect(description).toContain(`${t} Flow`);
  expect(description).toContain("1 link needs verification");
  expect(description).toContain("The table view lists the same records");

  // Line patterns, not colour alone: solid dependencies, dashed data flow (through its name's pill), dotted implements;
  // "?" marks Needs verification. Read from the Cytoscape instance behind the canvas.
  const edges = () => page.evaluate(() => {
    // Development builds put the Cytoscape instance on window.__softwardenGraph (src/features/graph/cy-graph.tsx).
    const cy = (window as Window & { __softwardenGraph?: import("cytoscape").Core }).__softwardenGraph;
    return cy?.edges().map((e) => ({ classes: e.classes().join(" "), style: String(e.style("line-style")), label: String(e.data("label")) })) ?? [];
  });
  await expect.poll(async () => (await edges()).length).toBeGreaterThan(0);
  const drawn = await edges();
  expect(drawn.filter((e) => e.classes.startsWith("dependency")).every((e) => e.style === "solid")).toBe(true);
  expect(drawn.filter((e) => e.classes.startsWith("flow")).map((e) => e.style)).toEqual(["dashed", "dashed"]);
  expect(drawn.filter((e) => e.classes === "implements").map((e) => e.style)).toEqual(["dotted"]);
  expect(drawn.filter((e) => e.label.startsWith("?"))).toHaveLength(1);
  const legend = page.getByRole("list", { name: "Legend" });
  await expect(legend).toContainText("Data flow, not operational");
  await expect(legend).toContainText("Needs verification");
  await expect(page.getByText(CAUTION)).toBeVisible();
  // The static SVG is the printed copy.
  await page.emulateMedia({ media: "print" });
  await expect(page.getByRole("img", { name: `Dependency graph for ${t} Target` }).locator("desc")).toContainText(`${t} Target is the target`);
  await page.emulateMedia({ media: "screen" });

  // The Table is the complete equivalent and one click away; the view is in the URL.
  await page.getByRole("tab", { name: "Table" }).click();
  await expect(page).toHaveURL(/view=table/);
  await expect(page.getByRole("table", { name: /Dependents and dependencies of/ })).toBeVisible();
  await page.getByRole("tab", { name: "Graph" }).click();
  await expect(page).toHaveURL(/view=graph/);
  await page.getByRole("button", { name: "Show table" }).click();
  await expect(page.getByRole("tab", { name: "Table" })).toHaveAttribute("aria-selected", "true");

  // Narrow screens and reduced motion default to the Table.
  await page.setViewportSize({ width: 600, height: 900 });
  await page.goto(`/dependencies?target=${s.target.id}`);
  await expect(page.getByRole("tab", { name: "Table" })).toHaveAttribute("aria-selected", "true");
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/dependencies?target=${s.target.id}`);
  await expect(page.getByRole("tab", { name: "Table" })).toHaveAttribute("aria-selected", "true");
  // An explicit ?view= wins.
  await page.goto(`/dependencies?target=${s.target.id}&view=graph`);
  await expect(page.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
});

test("Keyboard: tab strip arrows, radio arrows and a visible path from the table", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(page, t);
  await page.goto(`/dependencies?target=${s.target.id}&view=graph`);
  const graphTab = page.getByRole("tab", { name: "Graph" });
  await graphTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Table" })).toBeFocused();
  await expect(page).toHaveURL(/view=table/);

  await page.getByRole("group", { name: "Direction" }).getByLabel("Dependents").focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("group", { name: "Direction" }).getByLabel("Depends on")).toBeChecked();

  await page.getByRole("button", { name: `Show path for ${t} Library` }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: `Selected: ${t} Library` })).toBeVisible();
});
