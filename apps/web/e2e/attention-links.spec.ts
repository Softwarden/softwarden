import { expect, test, type Page } from "@playwright/test";
import { pageAs, personId, post, token, showAllClear } from "./helpers";

/** Links to verify and Stale assessments on Needs attention, with routing, scopes and states. */

test.describe.configure({ mode: "serial" });

/** A link that needs verification: confirmed, then the source's technical owner changed ("Owner changed since confirmation."). */
async function staleLink(page: Page, t: string, steward: "editor" | "none" = "editor") {
  const portal = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const sync = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  const link = await post(page, "relationships", { type: "depends_on", source: portal.id, target: sync.id });
  if (steward === "none") await post(page, `applications/${portal.id}`, { it_steward: null }, "PATCH");
  const dana = await personId(page, "Dana Ortiz");
  const priya = await personId(page, "Priya Raman");
  await post(page, `relationships/${link.id}/confirmations`, { confirmed_by: dana, evidence: "Email from Dana" });
  await post(page, `applications/${portal.id}`, { technical_owner: priya }, "PATCH");
  return { portal, sync, link, sentence: `${t} Portal depends on ${t} Sync` };
}

const section = (page: Page, name: string) => page.getByRole("region", { name: new RegExp(`^${name}`) });

test("Links to verify: Why text, routing to the steward, scopes in the URL, and the nav badge matches", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { portal, sentence } = await staleLink(editor, t);

  // Other specs add records for this user while this one runs, so serve one snapshot to the page and the nav badge.
  let snapshot: Promise<string> | null = null;
  await editor.route("**/api/v1/needs-attention?scope=mine", async (route) => {
    snapshot ??= route.fetch().then((r) => r.text());
    await route.fulfill({ contentType: "application/json", body: await snapshot });
  });
  await editor.goto("/");
  const links = section(editor, "Links to verify");
  const row = links.getByRole("row", { name: new RegExp(`${t} Portal depends on ${t} Sync`) });
  await expect(row.getByText("Needs verification")).toBeVisible();
  await expect(row.getByText("Owner changed since confirmation.")).toBeVisible();
  await expect(row.getByText(/^\d{4}-\d{2}-\d{2}$/)).toBeVisible();
  await expect(row.getByText("Sam Editor")).toBeVisible(); // routed to the source record's IT steward
  await expect(links.getByRole("table", { name: "Links to verify" })).toBeVisible();
  await expect(links).toContainText("Sorted by last confirmed, oldest first");

  // The page count and the navigation badge count the same items.
  const nav = await editor.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: /^Needs attention/ }).textContent();
  const badge = Number(/, (\d+) items/.exec(nav ?? "")![1]);
  await expect(editor.getByText(`${badge} ${badge === 1 ? "item" : "items"} derived from your catalog records.`)).toBeVisible();
  await expect(editor.getByRole("button", { name: /^Assigned to me/ })).toContainText(String(badge));

  await editor.unroute("**/api/v1/needs-attention?scope=mine");

  // Next step: the source record's Relationships tab, where Confirm link lives.
  await row.getByRole("link", { name: `Confirm link for ${sentence}` }).click();
  await expect(editor).toHaveURL(new RegExp(`/applications/${portal.id}\\?tab=relationships`));
  await expect(editor.getByRole("button", { name: `Confirm link with ${t} Sync` })).toBeVisible();

  // The admin is not the steward and not the coordinator's fallback for this item: it is only in All.
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/");
  await expect(section(admin, "Links to verify").getByRole("row", { name: new RegExp(`${t} Portal`) })).toHaveCount(0);
  await admin.getByRole("button", { name: /^All/ }).click();
  await expect(admin).toHaveURL(/scope=all/);
  await expect(section(admin, "Links to verify").getByRole("row", { name: new RegExp(`${t} Portal`) })).toBeVisible();

  // Confirming clears it.
  const dana = await personId(editor, "Dana Ortiz");
  const links2 = await (await editor.request.get(`/api/v1/relationships?resource=${portal.id}`)).json();
  await post(editor, `relationships/${links2[0].id}/confirmations`, { confirmed_by: dana, evidence: "Re-confirmed" });
  await admin.reload();
  await expect(section(admin, "Links to verify").getByRole("row", { name: new RegExp(`${t} Portal`) })).toHaveCount(0);
});

test("Unassigned scope and coordinator routing; Viewers see View link", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await staleLink(editor, t, "none");
  const name = new RegExp(`${t} Portal`);

  // Not the editor's (no steward), but the coordinator (Alex Admin) catches unrouted items.
  await editor.goto("/");
  await expect(section(editor, "Links to verify").getByRole("row", { name })).toHaveCount(0);
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/");
  const row = section(admin, "Links to verify").getByRole("row", { name });
  await expect(row).toBeVisible();
  await expect(row.getByText("(coordinator)")).toBeVisible();

  // Unassigned lists items with no steward of their own, in the URL.
  await admin.getByRole("button", { name: /^Unassigned/ }).click();
  await expect(admin).toHaveURL(/scope=unassigned/);
  await expect(section(admin, "Links to verify").getByRole("row", { name })).toBeVisible();
  await admin.reload();
  await expect(admin.getByRole("button", { name: /^Unassigned/ })).toHaveAttribute("aria-pressed", "true");

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/?scope=all");
  const viewerRow = section(viewer, "Links to verify").getByRole("row", { name });
  await expect(viewerRow.getByRole("link", { name: /^View link for/ })).toBeVisible();
  await expect(viewerRow.getByRole("link", { name: /^Confirm link/ })).toHaveCount(0);
});

test("Stale assessments: routed to the owner, lands on /assessments/:id, leaves after a refresh", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const core = await post(editor, "applications", { name: `${t} Core`, lifecycle: "active" });
  const top = await post(editor, "components", { name: `${t} Top`, lifecycle: "active", component_type: "script" });
  const assessment = await post(editor, "assessments", { target: core.id, change_type: "retirement", title: `${t} Retire Core`, target_date: "2027-01-15" });

  await editor.goto("/");
  await expect(section(editor, "Stale assessments").getByRole("row", { name: new RegExp(`${t} Retire`) })).toHaveCount(0); // fresh

  await post(editor, "relationships", { type: "depends_on", source: top.id, target: core.id }); // a new dependent
  await editor.goto("/");
  const stale = section(editor, "Stale assessments");
  const row = stale.getByRole("row", { name: new RegExp(`${t} Retire Core`) });
  await expect(row.getByText(/Stale since \d{4}-\d{2}-\d{2}/)).toBeVisible();
  await expect(row.getByText(/A depends_on link was added/)).toBeVisible();
  await expect(row.getByText("2027-01-15")).toBeVisible();
  await expect(row.getByText("Sam Editor")).toBeVisible(); // the assessment owner
  await expect(stale).toContainText("Sorted by target date");

  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/");
  await expect(section(admin, "Stale assessments").getByRole("row", { name: new RegExp(`${t} Retire`) })).toHaveCount(0);

  await row.getByRole("link", { name: `Open and refresh for ${t} Retire Core` }).click();
  await expect(editor).toHaveURL(new RegExp(`/assessments/${assessment.id}$`));
  await expect(editor.getByRole("heading", { name: `${t} Retire Core`, level: 1 })).toBeVisible();
  await expect(editor.getByText(/A depends_on link was added/)).toBeVisible();

  await post(editor, `assessments/${assessment.id}/refresh`, {});
  await editor.goto("/");
  await expect(section(editor, "Stale assessments").getByRole("row", { name: new RegExp(`${t} Retire`) })).toHaveCount(0);
});

test("States: loading, error with retry, all clear and the empty catalog", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const body = (extra: object) => ({
    scope: "mine", records: 3, counts: { mine: 0, all: 0, unassigned: 0 },
    ownership_gaps: [], reviews: [], links_to_verify: [], stale_assessments: [], renewal_actions: [], renewal_gaps: [], discovery_candidates: [], ...extra,
  });

  // Loading, then error with Retry (one queue API, one failure; a retry succeeds).
  let failing = true; // the navigation badge asks too, so fail every request until the retry
  await editor.route("**/api/v1/needs-attention*", async (route) => {
    if (failing) return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Something broke." }) });
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(body({})) });
  });
  await editor.goto("/");
  await expect(editor.getByRole("alert")).toContainText("Something broke.");
  failing = false;
  await editor.getByRole("button", { name: "Retry" }).click();

  // All clear: each section says so in words, for the scope shown (Assigned to me), on the one Nothing waiting
  // line, opened.
  await expect(editor.getByTestId("all-clear")).toContainText("Nothing in Ownership gaps, AI agent gaps, Reviews");
  await showAllClear(editor);
  await expect(section(editor, "Links to verify")).toContainText("No links to verify are assigned to you.");
  await expect(section(editor, "Stale assessments")).toContainText("None of your open assessments is stale.");
  await expect(editor.getByRole("heading", { name: "Ownership gaps" })).toBeVisible();

  // A new install has no records at all.
  await editor.unroute("**/api/v1/needs-attention*");
  await editor.route("**/api/v1/needs-attention*", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body({ records: 0 })) }));
  await editor.goto("/");
  await expect(editor.getByText("Nothing needs attention yet")).toBeVisible();
});
