import { expect, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Global search. The command palette searches records, vendors, people and attachments through
 *  `GET search` (a vendor's name finds its applications and the AI agents whose models it makes), with snippets as text
 *  and "Search everything for …"; the /search page keeps the query, kinds and archived switch in the URL, groups by
 *  kind with counts and "Show all N", and says when matches are close rather than exact or a word was ignored. Snippet
 *  text from a record is never markup. Tokens keep these results apart from the other specs' records in Contoso. */

async function openPalette(page: Page) {
  await page.getByRole("button", { name: /Search or jump to/ }).click();
  const dialog = page.getByRole("dialog", { name: "Command palette" });
  await expect(dialog).toBeVisible();
  return dialog;
}

/** A word only this test uses: letters only (the search parser splits on punctuation and digits stay glued). */
const word = (t: string, prefix: string) => `${prefix}${t.replace(/\D/g, "").split("").map((d) => "abcdefghij"[Number(d)]).join("")}`;

// The palette journey was one test that took 10 s alone and over 60 s on a loaded machine (it
// timed out in full runs). It is three tests now, each with its own records, and these specs
// get the 120 s the other multi-step specs have (layout-and-content.spec.ts; 180 s for the serial ones).
test.describe.configure({ timeout: 120_000 });

/** A vendor (and an AI provider of the same name) whose name finds an application, the AI agent using its model, the
 *  vendor itself and an attachment, all named after this test's token. */
async function vendorWithRecords(page: Page) {
  const t = token();
  const company = word(t, "Zyl");
  const vendor = await post(page, "vendors", { name: `${company} Software` });
  const app = await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active", vendor: vendor.id });
  const provider = await post(page, "ai-providers", { name: `${company} Software` });
  const model = await post(page, "ai-models", { provider: provider.id, name: "Orbit", version: "2", modality: "text" });
  const agent = await post(page, "ai-agents", { name: `${t} Ledger Assistant`, lifecycle: "active", models_used: [model.id] });
  await post(page, `applications/${app.id}/attachments`, { url: "https://example.test/ledger-runbook", title: `${company} runbook`, category: "sop_runbook" });
  return { t, company, app, agent };
}

test("Palette: a vendor's name finds its applications, the agents using its models, the vendor and attachments; an attachment opens its record's tab", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const { t, company, app } = await vendorWithRecords(page);

  await page.goto("/");
  const palette = await openPalette(page);
  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/api/v1/search?") && r.url().includes(`q=${company}`)),
    palette.getByRole("combobox").fill(company),
  ]);
  expect(request.url()).toContain(`q=${company}`);
  const list = palette.getByRole("listbox");
  await expect(list.getByRole("group", { name: "Applications" }).getByRole("option", { name: new RegExp(`${t} Ledger`) })).toBeVisible();
  const agents = list.getByRole("group", { name: "AI agents" });
  await expect(agents.getByRole("option", { name: new RegExp(`${t} Ledger Assistant`) })).toBeVisible();
  await expect(agents.getByTestId("search-snippet").first()).toContainText(company); // matched through its model's maker
  await expect(list.getByRole("group", { name: "Vendors" }).getByRole("option", { name: new RegExp(`${company} Software`) })).toBeVisible();
  await expect(list.getByRole("group", { name: "Attachments" }).getByRole("option", { name: new RegExp(`${company} runbook · on ${t} Ledger`) })).toBeVisible();
  await expect(list.getByRole("option", { name: new RegExp(`Search everything for “${company}”`) })).toBeVisible();
  await expectNoViolations(page, "Command palette with search results");

  // The attachment opens its record's Attachments tab.
  await list.getByRole("group", { name: "Attachments" }).getByRole("option").first().click();
  await expect(page).toHaveURL(`/applications/${app.id}?tab=attachments`);
  await expect(page.getByRole("tab", { name: /^Attachments/ })).toHaveAttribute("aria-selected", "true");
  await page.context().close();
});

test("Palette: the best match opens; an AI agent opens its own page", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const { t, agent } = await vendorWithRecords(page);
  await page.goto("/");
  const palette = await openPalette(page);
  await palette.getByRole("combobox").fill(`${t} Ledger Assistant`);
  await expect(palette.getByRole("group", { name: "AI agents" }).getByRole("option").first()).toBeVisible();
  await palette.getByRole("group", { name: "AI agents" }).getByRole("option").first().click();
  await expect(page).toHaveURL(`/ai-agents/${agent.id}`);
  await page.context().close();
});

test("Palette: Search everything opens the results page with the query in the URL", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const { company } = await vendorWithRecords(page);
  await page.goto("/");
  const palette = await openPalette(page);
  await palette.getByRole("combobox").fill(company);
  await palette.getByRole("option", { name: new RegExp(`Search everything for “${company}”`) }).click();
  await expect(page).toHaveURL(`/search?q=${company}`);
  await expect(page.getByRole("heading", { level: 1, name: "Search" })).toBeVisible();
  await expect(page.getByRole("region", { name: /^Applications/ }).or(page.getByRole("heading", { level: 2, name: /^Applications/ })).first()).toBeVisible();
  await page.context().close();
});

test("Palette: a 429 says when to try again", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.route("**/api/v1/search?**", (route) => route.fulfill({ status: 429, headers: { "Retry-After": "30" }, contentType: "application/problem+json", body: JSON.stringify({ detail: "Request was throttled." }) }));
  await page.goto("/");
  const palette = await openPalette(page);
  await palette.getByRole("combobox").fill("payroll");
  await expect(palette.getByRole("alert")).toHaveText("Too many searches for now. Try again in 30 seconds.");
  // Pages and actions still work.
  await palette.getByRole("combobox").fill("Applications");
  await expect(palette.getByRole("group", { name: "Pages" }).getByRole("option", { name: /Applications/ })).toBeVisible();
  await page.context().close();
});

test("Results page: groups with counts, kinds and archived in the URL, Show all, close matches, ignored words", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const name = word(t, "Quill");
  for (let i = 1; i <= 12; i++) await post(page, "applications", { name: `${name} ${String(i).padStart(2, "0")}`, lifecycle: "active" });
  const old = await post(page, "components", { name: `${name} legacy job`, lifecycle: "active", component_type: "script" });
  await post(page, `components/${old.id}/archive`, {});
  await post(page, "people", { name: `${name} Person` });

  await page.goto(`/search?q=${name}`);
  await expect(page.getByRole("status").filter({ hasText: /results? for/ })).toContainText(`13 results for “${name}”`);
  const apps = page.getByRole("region", { name: /^Applications/ });
  await expect(apps.getByRole("heading", { level: 2 })).toContainText("12");
  await expect(apps.getByRole("listitem")).toHaveCount(10); // ten per kind by default
  await expect(page.getByRole("region", { name: /^Components/ })).toHaveCount(0); // archived: left out
  await expect(page.getByRole("region", { name: /^People/ }).getByRole("link", { name: `${name} Person` })).toHaveAttribute("href", "/admin/people");
  await expectNoViolations(page, "Search results page");

  // Show all: that kind alone, the API's largest page, in the URL.
  await apps.getByRole("link", { name: "Show all 12 applications" }).click();
  await expect(page).toHaveURL(new RegExp(`/search\\?q=${name}&kinds=application&limit=100$`));
  await expect(page.getByRole("region", { name: /^Applications/ }).getByRole("listitem")).toHaveCount(12);
  await expect(page.getByRole("group", { name: "Kinds" }).getByRole("button", { name: "Applications" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: /^People/ })).toHaveCount(0);

  // Kinds and archived are URL state: choose components and include archived.
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).toHaveURL(`/search?q=${name}`);
  await page.getByRole("group", { name: "Kinds" }).getByRole("button", { name: "Components" }).click();
  await page.getByLabel(/Include archived/).check();
  await expect(page).toHaveURL(new RegExp(`kinds=component`));
  await expect(page).toHaveURL(/include_archived=true/);
  const comps = page.getByRole("region", { name: /^Components/ });
  await expect(comps.getByRole("listitem")).toHaveCount(1);
  await expect(comps.getByRole("listitem")).toContainText("Archived");
  await page.reload();
  await expect(page.getByRole("region", { name: /^Components/ }).getByRole("listitem")).toHaveCount(1);
  await expect(page.getByLabel(/Include archived/)).toBeChecked();
  await expectNoViolations(page, "Search results page, filtered");

  // A typo finds close matches, said so.
  const typo = `${name.slice(0, -1)}${name.endsWith("z") ? "y" : "z"}`; // the last letter mistyped (always a different letter)
  await page.goto(`/search?q=${typo}`);
  await expect(page.getByTestId("search-fuzzy")).toContainText("Showing close matches");
  await expect(page.getByRole("region", { name: /^Applications/ }).getByRole("listitem").first()).toContainText(name);

  // A word with nothing to match in is ignored, and named.
  await page.goto(`/search?q=${encodeURIComponent(`${name} ²`)}`);
  await expect(page.getByTestId("search-ignored")).toContainText("Ignored: ²");
  await expect(page.getByRole("region", { name: /^Applications/ })).toBeVisible();

  // Nothing at all.
  await page.goto(`/search?q=${word(t, "Nothingmatches")}xyzzy`);
  await expect(page.getByText(/^No results for/)).toBeVisible();
  await page.context().close();
});

test("Snippets are text: markup stored in a description is shown, never run", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const needle = word(t, "Marker");
  await post(page, "applications", {
    name: `${t} <b>Bold</b> & <img src=x onerror="window.__xss=1">`, lifecycle: "active",
    purpose: `Reads ${needle} files. <img src=x onerror="window.__xss=2"> <script>window.__xss=3</script> "quoted" & done`,
  });
  await page.goto(`/search?q=${needle}`);
  const row = page.getByRole("region", { name: /^Applications/ }).getByRole("listitem").first();
  await expect(row.getByRole("link")).toHaveText(`${t} <b>Bold</b> & <img src=x onerror="window.__xss=1">`);
  const snippet = row.getByTestId("search-snippet");
  await expect(snippet).toContainText("Purpose:");
  await expect(snippet.locator("mark")).toHaveText(needle);
  await expect(row.locator("img, script, b")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();

  // The same in the palette.
  const palette = await openPalette(page);
  await palette.getByRole("combobox").fill(needle);
  const option = palette.getByRole("group", { name: "Applications" }).getByRole("option").first();
  await expect(option).toContainText("<b>Bold</b>");
  await expect(option.locator("mark")).toHaveText(needle);
  await expect(palette.locator("img, script, b")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  await page.context().close();
});

test("Viewer: search works read-only, and the empty page explains what to type", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "viewer@example.test");
  await page.goto("/search");
  await expect(page.getByText("Search the whole catalog")).toBeVisible();
  await expectNoViolations(page, "Search page, empty, Viewer");
  await page.getByLabel("Search everything").fill("Dana");
  await page.getByLabel("Search everything").press("Enter");
  await expect(page).toHaveURL("/search?q=Dana");
  await expect(page.getByRole("region", { name: /^People/ })).toContainText("Dana Ortiz");
  await page.context().close();
});
