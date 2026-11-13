import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations, showTheme } from "./axe";
import { pageAs, post, token } from "./helpers";

/** A record's Map tab (Cytoscape picture plus the grouped table, "What depends on this") and the
 *  dependency explorer on Cytoscape (depth 1 to 10 or all, links that close a loop, flows' direction and
 *  authentication). The acceptance check: "what depends on this app registration" is one click from the application's Map tab,
 *  and the same answer comes from the records' own /dependencies endpoints. */

// axe runs in both themes at each check (e2e/axe.ts).
test.describe.configure({ mode: "serial", timeout: 180_000 });

type Rec = { id: string; name: string };

/** Portal depends on the App registration (a component); Billing depends on it too, and Kiosk depends on Portal. Portal
 *  has a Production instance and a two-way OAuth integration to Payroll; Legacy (archived) and Sunset (retired) depend
 *  on Portal. */
async function scene(page: Page, t: string) {
  const portal: Rec = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const appReg: Rec = await post(page, "components", { name: `${t} App registration`, lifecycle: "active", component_type: "service" });
  const billing: Rec = await post(page, "applications", { name: `${t} Billing`, lifecycle: "active" });
  const kiosk: Rec = await post(page, "applications", { name: `${t} Kiosk`, lifecycle: "active" });
  const payroll: Rec = await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  const legacy: Rec = await post(page, "applications", { name: `${t} Legacy`, lifecycle: "active" });
  const sunset: Rec = await post(page, "applications", { name: `${t} Sunset`, lifecycle: "active" });
  const production: Rec = await post(page, `applications/${portal.id}/instances`, { name: `${t} Production`, environment: "production" });
  await post(page, "relationships", { type: "depends_on", source: portal.id, target: appReg.id });
  await post(page, "relationships", { type: "depends_on", source: billing.id, target: appReg.id });
  await post(page, "relationships", { type: "depends_on", source: kiosk.id, target: portal.id });
  await post(page, "relationships", { type: "depends_on", source: legacy.id, target: portal.id });
  await post(page, "relationships", { type: "depends_on", source: sunset.id, target: portal.id });
  const feed: Rec = await post(page, "integrations", {
    name: `${t} Feed`, lifecycle: "active", source: portal.id, target: payroll.id, protocol: "REST", direction: "two_way", auth_method: "oauth",
  });
  await post(page, `applications/${legacy.id}/archive`, {});
  await post(page, `applications/${sunset.id}`, { lifecycle: "retired" }, "PATCH");
  return { portal, appReg, billing, kiosk, payroll, legacy, sunset, production, feed };
}

/** The Cytoscape instance behind the picture: development builds (which the suite runs) put it on
 *  `window.__softwardenGraph` (src/features/graph/cy-graph.tsx). */
type GraphWindow = Window & { __softwardenGraph?: import("cytoscape").Core };
async function cyNodes(page: Page) {
  return page.evaluate(() => {
    const cy = (window as GraphWindow).__softwardenGraph;
    if (!cy) return null;
    return cy.nodes().map((n) => ({ id: n.id(), label: String(n.data("label")), border: String(n.style("border-style")), classes: n.classes().join(" ") }));
  });
}
async function cyEdges(page: Page) {
  return page.evaluate(() => {
    const cy = (window as GraphWindow).__softwardenGraph;
    if (!cy) return null;
    return cy.edges().map((e) => ({ id: e.id(), label: String(e.data("label")), classes: e.classes().join(" "), dash: String(e.style("line-style")), color: String(e.style("line-color")).replace(/\s/g, "") }));
  });
}
/** Click a node of the picture where Cytoscape drew it. */
async function clickNode(page: Page, id: string) {
  const at = await page.evaluate((nodeId) => {
    const box = document.querySelector("[data-testid=graph-canvas]") as HTMLElement;
    const p = (window as GraphWindow).__softwardenGraph!.getElementById(nodeId).renderedPosition();
    const r = box.getBoundingClientRect();
    return { x: r.left + p.x, y: r.top + p.y };
  }, id);
  await page.mouse.click(at.x, at.y);
}

const DISCOVERY = "aaaaaaaa-0000-4000-8000-000000000001";
const CONNECTOR = "bbbbbbbb-0000-4000-8000-000000000002";

test("Map tab: the picture and the grouped table show every connection; What depends on this answers in one click, as the API does", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(page, t);

  // A discovery source, added to the real response: linking one needs a connector run, which the discovery spec owns.
  await page.route(`**/api/v1/applications/${s.portal.id}/map*`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.nodes.push({ id: DISCOVERY, kind: "discovery_source", name: `${t} Portal (Entra)`, lifecycle: null, archived: false, retired: false, path: `discovery/observations/${DISCOVERY}`, map_path: null, expandable: false, detail: { connector: { id: CONNECTOR, name: "Entra", source: "entra" }, decision: "linked" }, hop: 1, root: false });
    body.edges.push({ id: `discovery:${DISCOVERY}`, source: s.portal.id, target: DISCOVERY, relation: "discovery", category: "discovery", label: "Entra", reads: "Discovered in", reads_inverse: "Source of", archived: false, retired: false, detail: { decision: "linked", gone: false } });
    body.rows.push({ edge: `discovery:${DISCOVERY}`, category: "discovery", near: s.portal.id, other: DISCOVERY, reads: "Discovered in", hop: 1 });
    body.counts.discovery = (body.counts.discovery ?? 0) + 1;
    await route.fulfill({ response, json: body });
  });

  await page.goto(`/applications/${s.portal.id}`);
  await page.getByRole("tab", { name: "Map" }).click();
  await expect(page).toHaveURL(/tab=map/);
  const picture = page.getByRole("application", { name: `Map of what ${t} Portal is connected to` });
  await expect(picture).toBeVisible();
  await expect(page.getByRole("button", { name: "1 step" })).toHaveAttribute("aria-pressed", "true");

  // The table: grouped by category with counts, rows read from the record, each linking to the other end.
  const table = page.getByRole("table", { name: /Everything .* Portal is connected to/ });
  const groups = table.locator('th[scope="rowgroup"]');
  await expect(groups).toHaveText(["Dependencies (4)", "Integrations (1)", "Instances (1)", "Discovery sources (1)"]);
  const row = (name: string) => table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });
  await expect(row(`${t} App registration`)).toContainText("Depends on");
  await expect(row(`${t} App registration`).getByRole("link", { name: `${t} App registration` })).toHaveAttribute("href", `/components/${s.appReg.id}`);
  await expect(row(`${t} Kiosk`)).toContainText("Is needed by");
  await expect(row(`${t} Legacy`)).toContainText("Archived");
  await expect(row(`${t} Sunset`)).toContainText("Retired");
  const payroll = row(`${t} Payroll`);
  await expect(payroll).toContainText("Exchanges data with");
  await expect(payroll.getByRole("link", { name: `${t} Feed` })).toHaveAttribute("href", `/integrations/${s.feed.id}`);
  await expect(row(`${t} Production`).getByRole("link", { name: `${t} Production` })).toHaveAttribute("href", `/applications/${s.portal.id}?tab=instances`);
  const source = row(`${t} Portal \\(Entra\\)`);
  await expect(source).toContainText("Discovered in");
  await expect(source.getByRole("link")).toHaveAttribute("href", new RegExp(`^/discovery\\?status=all&connector=${CONNECTOR}&q=`));

  // The picture has every node; archived and retired have a dashed border and the word.
  await expect.poll(async () => (await cyNodes(page))?.length).toBe(8);
  const nodes = (await cyNodes(page))!;
  expect(nodes.map((n) => n.id).sort()).toEqual([s.portal.id, s.appReg.id, s.kiosk.id, s.legacy.id, s.sunset.id, s.payroll.id, s.production.id, DISCOVERY].sort());
  const legacy = nodes.find((n) => n.id === s.legacy.id)!;
  expect(legacy.border).toBe("dashed");
  expect(legacy.label).toContain("Archived");
  expect(nodes.find((n) => n.id === s.sunset.id)!.label).toContain("Retired");
  expect(nodes.find((n) => n.id === s.portal.id)!.classes).toContain("root");
  const edges = (await cyEdges(page))!;
  expect(edges.find((e) => e.id === `integration:${s.feed.id}`)!.classes).toContain("two-way");

  // One click: What depends on the App registration.
  await row(`${t} App registration`).getByRole("button", { name: new RegExp(`^What depends on this\\s*, ${t} App registration$`) }).click();
  await expect(page).toHaveURL(new RegExp(`dependents=${s.appReg.id}`));
  const answer = page.getByRole("region", { name: `What depends on ${t} App registration` });
  await expect(answer.getByRole("heading", { level: 3 })).toBeFocused();
  const listed = answer.getByRole("list", { name: `Records that depend on ${t} App registration` });
  // Billing and Portal directly; Kiosk and the archived Legacy through Portal; the retired Sunset is left out, and said so.
  await expect(listed.getByRole("listitem")).toHaveCount(4);
  await expect(answer).toContainText(`4 records depend on ${t} App registration, directly or through others (1 retired left out)`);
  await expect(listed.getByRole("listitem").filter({ hasText: `${t} Legacy` })).toContainText("Archived");
  await expect(listed.getByRole("listitem").filter({ hasText: `${t} Kiosk` })).toContainText(`${t} Kiosk → depends on → ${t} Portal → depends on → ${t} App registration`);
  // The row is highlighted and the node picked.
  await expect(row(`${t} App registration`)).toHaveAttribute("aria-selected", "true");
  await expect(row(`${t} App registration`)).toContainText("Selected");
  await expect.poll(async () => (await cyNodes(page))!.find((n) => n.id === s.appReg.id)!.classes).toContain("picked");

  // The same answer from the API: the component's own endpoint, and the application's (what it depends on).
  const names = (await listed.getByRole("listitem").getByRole("link").allTextContents()).sort();
  const fromComponent = await (await page.request.get(`/api/v1/components/${s.appReg.id}/dependencies?depth=all`)).json();
  expect(fromComponent.nodes.filter((n: { cycle: boolean }) => !n.cycle).map((n: { name: string }) => n.name).sort()).toEqual(names);
  expect(names).toEqual([`${t} Billing`, `${t} Kiosk`, `${t} Legacy`, `${t} Portal`]);
  const fromApplication = await (await page.request.get(`/api/v1/applications/${s.portal.id}/dependencies?depth=all&direction=depends_on`)).json();
  expect(fromApplication.nodes.map((n: { name: string }) => n.name)).toContain(`${t} App registration`);
  await expect(answer.getByRole("link", { name: /^Open in Dependencies/ })).toHaveAttribute("href", `/dependencies?target=${s.appReg.id}&depth=all`);

  // The record itself (the app registration recorded on the application): one click on the toolbar's button; the
  // answer matches GET /applications/{id}/dependencies?depth=all.
  const askRoot = page.getByRole("button", { name: `What depends on ${t} Portal`, exact: true });
  await askRoot.click();
  await expect(page).toHaveURL(new RegExp(`dependents=${s.portal.id}`));
  const own = page.getByRole("region", { name: `What depends on ${t} Portal` });
  await expect(own.getByRole("heading", { level: 3 })).toBeFocused();
  await expect(own.getByRole("list").getByRole("listitem")).toHaveCount(2);
  const ownNames = (await own.getByRole("list").getByRole("listitem").getByRole("link").allTextContents()).sort();
  const fromPortal = await (await page.request.get(`/api/v1/applications/${s.portal.id}/dependencies?depth=all`)).json();
  expect(fromPortal.nodes.filter((n: { cycle: boolean }) => !n.cycle).map((n: { name: string }) => n.name).sort()).toEqual(ownNames);
  expect(ownNames).toEqual([`${t} Kiosk`, `${t} Legacy`]); // retired left out by default, as in the explorer
  await expect(own).toContainText("1 retired left out");

  // Focus: each panel's own action focuses that panel; closing one returns focus to what opened it.
  const showKiosk = row(`${t} Kiosk`).getByRole("button", { name: new RegExp(`^Show on map\\s*, ${t} Kiosk$`) });
  await showKiosk.click();
  await expect(page.getByRole("heading", { name: `Selected: ${t} Kiosk` })).toBeFocused(); // not the open answer's heading
  await page.getByRole("region", { name: `Selected: ${t} Kiosk` }).getByRole("button", { name: "Clear selection" }).click();
  await expect(page.getByRole("region", { name: /^Selected: / })).toHaveCount(0);
  await expect(showKiosk).toBeFocused();
  await own.getByRole("button", { name: /^Close/ }).click();
  await expect(own).toHaveCount(0);
  await expect(askRoot).toBeFocused();
  // Opened from a row: Close returns to that row's button.
  const askKiosk = row(`${t} Kiosk`).getByRole("button", { name: new RegExp(`^What depends on this\\s*, ${t} Kiosk$`) });
  await askKiosk.click();
  await page.getByRole("region", { name: `What depends on ${t} Kiosk` }).getByRole("button", { name: /^Close/ }).click();
  await expect(askKiosk).toBeFocused();

  // A `dependents` record that isn't on this map (Billing is two steps out) still gets its answer, fetched by ID.
  await page.goto(`/applications/${s.portal.id}?tab=map&dependents=${s.billing.id}`);
  await expect(page.getByRole("region", { name: `What depends on ${t} Billing` })).toContainText(`Nothing is recorded as depending on ${t} Billing, at any depth.`);

  // Clicking a node selects it: Open, Center map here, What depends on this.
  await page.goto(`/applications/${s.portal.id}?tab=map`);
  await expect.poll(async () => (await cyNodes(page))?.length).toBe(8); // the injected discovery source too (the route stays)
  await clickNode(page, s.appReg.id);
  const selected = page.getByRole("region", { name: `Selected: ${t} App registration` });
  await expect(selected).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`node=${s.appReg.id}`));
  await expect(selected.getByRole("link", { name: /^Open/ })).toHaveAttribute("href", `/components/${s.appReg.id}`);
  await selected.getByRole("button", { name: /^What depends on this/ }).click();
  await expect(page.getByRole("region", { name: `What depends on ${t} App registration` }).getByRole("listitem")).toHaveCount(4);

  // Keyboard: focus the picture, arrows move a ring (announced), Enter selects.
  await picture.focus();
  await page.keyboard.press("Home");
  await expect(page.locator("[aria-live=polite]").filter({ hasText: "Enter selects it" })).toContainText(`${t} Portal, Application · this record`);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("region", { name: `Selected: ${t} Portal` })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`node=${s.portal.id}`));
  await expect(picture).toBeFocused();
  // The hidden instructions name every key, and where the equivalent table is.
  const instructions = await picture.evaluate((el) => (el.getAttribute("aria-describedby") ?? "").split(" ").map((id) => document.getElementById(id)?.textContent ?? "").join(" "));
  for (const words of ["Enter or Space selects", "Escape clears the focus ring", "Tab leaves the picture", "Connections table beside it"]) expect(instructions).toContain(words);
  // Escape clears the focus ring and keeps focus; Tab then leaves the picture (it never traps focus).
  const ringed = async () => (await cyNodes(page))?.filter((n) => n.classes.split(" ").includes("kbd")).length;
  await page.keyboard.press("ArrowRight");
  await expect.poll(ringed).toBe(1);
  await page.keyboard.press("Escape");
  await expect.poll(ringed).toBe(0);
  await expect(picture).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect.poll(ringed).toBe(1);
  await page.keyboard.press("Tab");
  await expect(picture).not.toBeFocused();
  await expect.poll(ringed).toBe(0);

  // Center map here: the other record's own map.
  await page.goto(`/applications/${s.portal.id}?tab=map&node=${s.appReg.id}`);
  await page.getByRole("region", { name: `Selected: ${t} App registration` }).getByRole("link", { name: /^Center map here/ }).click();
  await expect(page).toHaveURL(new RegExp(`/components/${s.appReg.id}\\?tab=map`));
  await expect(page.getByRole("heading", { name: `${t} App registration`, level: 1 })).toBeVisible();
  await expect(page.getByRole("table", { name: /Everything .* App registration is connected to/ }).getByRole("rowheader", { name: new RegExp(`^${t} Billing`) })).toBeVisible();
});

test("Map tab: depth 2 is in the URL; viewers read it all; empty, error, loading and truncated states", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(editor, t);

  await editor.goto(`/applications/${s.portal.id}?tab=map&depth=2`);
  await expect(editor.getByRole("button", { name: "2 steps" })).toHaveAttribute("aria-pressed", "true");
  const table = editor.getByRole("table", { name: /up to two steps away/ });
  const billing = table.getByRole("row").filter({ has: editor.getByRole("rowheader", { name: new RegExp(`^${t} Billing`) }) });
  await expect(billing).toContainText(`through ${t} App registration`);
  await expect(billing).toContainText(`${t} App registration is needed by`);
  await expect(table.locator('th[scope="rowgroup"]').first()).toHaveText(/^Dependencies \(4\), and 1 two steps away$/);
  await editor.getByRole("button", { name: "1 step" }).click();
  await expect(editor).not.toHaveURL(/depth=2/);
  await expect(editor.getByRole("rowheader", { name: new RegExp(`^${t} Billing`) })).toHaveCount(0);

  // A Viewer reads the same map and the same answer; nothing to change.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${s.portal.id}?tab=map&depth=2&node=${s.appReg.id}&dependents=${s.appReg.id}`);
  await expect(viewer.getByRole("application", { name: `Map of what ${t} Portal is connected to` })).toBeVisible();
  await expect(viewer.getByRole("region", { name: `What depends on ${t} App registration` }).getByRole("listitem")).toHaveCount(4);
  await expect(viewer.getByRole("region", { name: `Selected: ${t} App registration` })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Record review" })).toHaveCount(0);

  // Empty: no connections yet; Editors get Add relationship, Viewers don't.
  const lonely: Rec = await post(editor, "applications", { name: `${t} Lonely`, lifecycle: "active" });
  await editor.goto(`/applications/${lonely.id}?tab=map`);
  await expect(editor.getByText("No connections yet")).toBeVisible();
  await expect(editor.getByRole("link", { name: "Add relationship" })).toHaveAttribute("href", `/applications/${lonely.id}?tab=relationships`);
  await viewer.goto(`/applications/${lonely.id}?tab=map`);
  await expect(viewer.getByText("No connections yet")).toBeVisible();
  await expect(viewer.getByRole("link", { name: "Add relationship" })).toHaveCount(0);

  // Error with Retry, then the loading line while a slow response is pending.
  let failing = true;
  await editor.route(`**/api/v1/applications/${s.portal.id}/map*`, async (route) => {
    if (failing) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Boom." }) });
    await new Promise((r) => setTimeout(r, 700));
    return route.continue();
  });
  await editor.goto(`/applications/${s.portal.id}?tab=map`);
  await expect(editor.getByRole("alert")).toContainText("Couldn't load the map. Boom.");
  failing = false;
  await editor.getByRole("button", { name: "Retry" }).click();
  await expect(editor.getByText("Loading the map…")).toBeVisible();
  await expect(editor.getByRole("application", { name: `Map of what ${t} Portal is connected to` })).toBeVisible();
  await editor.unroute(`**/api/v1/applications/${s.portal.id}/map*`);

  // A depth change that fails says so and keeps the map it had, with Retry.
  await editor.route(`**/api/v1/applications/${s.portal.id}/map?depth=2`, (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Boom." }) }));
  await editor.goto(`/applications/${s.portal.id}?tab=map`);
  await expect(editor.getByRole("table", { name: /Everything .* Portal is connected to/ })).toBeVisible();
  await editor.getByRole("button", { name: "2 steps" }).click();
  await expect(editor.getByRole("alert")).toContainText("Couldn't load the map at 2 steps. Boom. The map below is the one shown before.");
  await expect(editor.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(editor.getByRole("rowheader", { name: new RegExp(`^${t} App registration`) })).toBeVisible();
  await editor.unroute(`**/api/v1/applications/${s.portal.id}/map?depth=2`);

  // Truncated: the notice, and counts from before the cap.
  await editor.route(`**/api/v1/applications/${s.portal.id}/map*`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, truncated: true, counts: { ...body.counts, dependency: 212 } } });
  });
  await editor.goto(`/applications/${s.portal.id}?tab=map`);
  await expect(editor.getByText(/The map stops at 150 items, so some connections aren't shown/)).toBeVisible();
  await expect(editor.getByRole("status").filter({ hasText: "Dependencies: 212" })).toBeVisible();
});

test("Map tab and explorer pass axe in both themes; the canvas takes its colors from the tokens", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const s = await scene(page, t);
  await page.goto(`/applications/${s.portal.id}?tab=map&depth=2&node=${s.appReg.id}&dependents=${s.appReg.id}`);
  await expect(page.getByRole("region", { name: `What depends on ${t} App registration` }).getByRole("listitem")).toHaveCount(4);
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Map tab with a selection and its dependents");

  // The integration's line is the line blue in each theme, re-read when the theme changes.
  const flow = async () => (await cyEdges(page))!.find((e) => e.id === `integration:${s.feed.id}`)!.color;
  await showTheme(page, "light");
  await expect.poll(flow).toBe("rgb(36,87,197)");
  await showTheme(page, "dark");
  await expect.poll(flow).toBe("rgb(143,178,248)");
  await showTheme(page, "light");

  await page.goto(`/dependencies?target=${s.appReg.id}&depth=all&view=graph`);
  await expect(page.getByRole("application", { name: `Dependency graph for ${t} App registration` })).toBeVisible();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Dependencies, Cytoscape graph");
  await page.goto(`/dependencies?target=${s.appReg.id}&depth=all&view=table`);
  await page.getByRole("button", { name: `Show path for ${t} Kiosk` }).click();
  await page.waitForLoadState("networkidle");
  await expectNoViolations(page, "Dependencies, table and path panel");
});

test("Explorer: depth 3 and all, a link that closes a loop, include retired, the path panel, flows' direction and authentication, all in the URL", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const target: Rec = await post(page, "applications", { name: `${t} Target`, lifecycle: "active" });
  const chain: Rec[] = [];
  for (const i of [1, 2, 3, 4]) {
    chain.push(await post(page, "applications", { name: `${t} Level${i}`, lifecycle: "active" }));
    await post(page, "relationships", { type: "depends_on", source: chain[i - 1].id, target: (i === 1 ? target : chain[i - 2]).id });
  }
  // Target depends on Level2: Target -> Level2 -> Level1 -> Target closes a loop.
  const loop = await post(page, "relationships", { type: "depends_on", source: target.id, target: chain[1].id });
  const old: Rec = await post(page, "applications", { name: `${t} Old`, lifecycle: "active" });
  await post(page, "relationships", { type: "depends_on", source: old.id, target: target.id });
  await post(page, `applications/${old.id}`, { lifecycle: "retired" }, "PATCH");
  const peer: Rec = await post(page, "applications", { name: `${t} Peer`, lifecycle: "active" });
  await post(page, "integrations", { name: `${t} Sync feed`, lifecycle: "active", source: target.id, target: peer.id, protocol: "SFTP", direction: "two_way", auth_method: "oauth" });

  await page.goto(`/dependencies?view=table&target=${target.id}&depth=3`);
  const depth = page.getByLabel("Depth");
  await expect(depth).toHaveValue("3");
  const table = page.getByRole("table", { name: /Dependents of .* Target/ });
  const row = (name: string) => table.getByRole("row", { name: new RegExp(`^${name}`) });
  await expect(row(`${t} Level3`)).toContainText(`${t} Level3 → depends on → ${t} Level2 → depends on → ${t} Level1 → depends on → ${t} Target`);
  await expect(row(`${t} Level4`)).toHaveCount(0);

  // The link back to the target closes a loop: chip, row note, summary; it matches the response's `cycles`.
  const api = await (await page.request.get(`/api/v1/dependencies?target=${target.id}&depth=3`)).json();
  expect(api.cycles.map((c: { relationship: string }) => c.relationship)).toContain(loop.id);
  const loopRow = table.getByRole("row", { name: new RegExp(`^${t} Target Closes a loop`) });
  await expect(loopRow).toBeVisible();
  await expect(loopRow).toContainText(`Leads back to ${t} Target, already on this path`);
  await expect(page.getByRole("status").filter({ hasText: "1 link closes a loop." })).toBeVisible();

  // Flows: direction and authentication.
  const flow = table.getByRole("row", { name: new RegExp(`^${t} Sync feed`) });
  await expect(flow).toContainText("Both ways");
  await expect(flow).toContainText("Authentication: OAuth 2.0");

  // All levels: Level4 too. Include retired: Old.
  await depth.selectOption("all");
  await expect(page).toHaveURL(/depth=all/);
  await expect(row(`${t} Level4`)).toBeVisible();
  await expect(row(`${t} Old`)).toHaveCount(0);
  await page.getByLabel("Include retired").check();
  await expect(page).toHaveURL(/retired=1/);
  await expect(row(`${t} Old`).getByText("Retired", { exact: true })).toBeVisible();

  // The path panel.
  await page.getByRole("button", { name: `Show path for ${t} Level4` }).click();
  const panel = page.getByRole("complementary", { name: "Selected record" });
  await expect(panel.getByRole("list", { name: /Path from .* Target to .* Level4/ }).getByRole("listitem")).toHaveCount(5);

  // The URL round-trip: reload keeps every filter.
  await page.reload();
  await expect(page.getByLabel("Depth")).toHaveValue("all");
  await expect(page.getByLabel("Include retired")).toBeChecked();
  await expect(row(`${t} Old`)).toBeVisible();

  // The graph: the loop is drawn dash-dot and labelled; a click selects a record for the path panel.
  await page.getByRole("tab", { name: "Graph" }).click();
  await expect(page).toHaveURL(/view=graph/);
  await expect(page.getByRole("application", { name: `Dependency graph for ${t} Target` })).toBeVisible();
  await expect.poll(async () => (await cyEdges(page))?.filter((e) => e.classes.includes("cycle")).length ?? 0).toBeGreaterThan(0);
  const loopEdge = (await cyEdges(page))!.find((e) => e.classes.includes("cycle"))!;
  expect(loopEdge.label).toContain("closes a loop");
  expect(loopEdge.dash).toBe("dashed");
  const flowEdges = (await cyEdges(page))!.filter((e) => e.classes.includes("flow"));
  expect(flowEdges.length).toBe(2); // to and from the flow's pill
  await expect(page.getByRole("list", { name: "Legend" })).toContainText("Closes a loop");
  await clickNode(page, `dependents:${chain[1].id}`);
  await expect(page.getByRole("heading", { name: `Selected: ${t} Level2` })).toBeVisible();
  const picked = (await cyNodes(page))!.find((n) => n.id === `dependents:${chain[1].id}`)!;
  expect(picked.classes).toContain("picked");
  // The retired record has a dashed border and the word.
  const oldNode = (await cyNodes(page))!.find((n) => n.id === `dependents:${old.id}`)!;
  expect(oldNode.border).toBe("dashed");
  expect(oldNode.label).toContain("Retired");
});
