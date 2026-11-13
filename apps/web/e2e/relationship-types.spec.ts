import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { ANY_REVISION_FOR, pageAs, post, token } from "./helpers";

/** Relationship types (/relationship-types) and the inverse labels on links, plus the integration fields
 *  (direction, authentication, data classification, monitoring URL) on the form, the detail page and the list filters.
 *
 *  Types are organization-wide and other specs run beside these, so the tests only add types with unique names, delete
 *  what they add, and never rename a core type. */

/** The API's derived slug for a name. */
const slugOf = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

async function apiStatus(page: Page, path: string, method: string) {
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const res = await page.request.fetch(`/api/v1/${path}`, { method, headers: { "X-CSRFToken": csrf, ...ANY_REVISION_FOR(method) } });
  return { status: res.status(), body: res.status() === 204 ? null : await res.json() };
}

test("An Admin adds a type, uses it on a link that reads both ways, keeps it on edit, is refused delete with the count, and deletes it once unused", async ({ browser, baseURL }) => {
  const t = token();
  const name = `Feeds ${t}`;
  const inverse = `Is fed by ${t}`;
  const slug = slugOf(name);
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const a = await post(admin, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(admin, "applications", { name: `${t} B`, lifecycle: "active" });

  await admin.goto("/relationship-types");
  await expect(admin.getByRole("heading", { level: 1, name: "Relationship types" })).toBeVisible();
  await expect(admin.getByRole("navigation").getByRole("link", { name: "Relationship types" })).toBeVisible(); // Admin section
  const table = admin.getByRole("region", { name: "Relationship types table" });
  const row = (n: string) => table.getByRole("row", { name: new RegExp(`^${n}`) });
  // Core types: locked chip, no delete, and whether each counts as a dependency in words.
  await expect(row("Depends on")).toContainText("Is needed by");
  await expect(row("Depends on")).toContainText("Core");
  await expect(row("Depends on")).toContainText("Dependency");
  await expect(row("Replaces")).toContainText("Not a dependency");
  await expect(row("Depends on")).toContainText("Core: can't be deleted");
  await expect(table.getByRole("button", { name: "Delete Depends on" })).toHaveCount(0);

  // Add: the slug follows the name, the preview reads both ways.
  const add = admin.getByRole("button", { name: "Add relationship type" });
  await add.click();
  const dialog = admin.getByRole("dialog", { name: "Add relationship type" });
  await dialog.getByRole("button", { name: "Add relationship type" }).click();
  await expect(dialog.getByLabel("Name (required)")).toBeFocused(); // the first invalid field
  await expect(dialog.getByRole("alert").first()).toContainText("Enter a name");
  await dialog.getByLabel("Name (required)").fill(name);
  await dialog.getByLabel("Inverse name").fill(inverse);
  await dialog.getByLabel("Description").fill("Pushes records downstream.");
  await expect(dialog.getByLabel("Slug")).toHaveValue(slug);
  await expect(dialog).toContainText(`Portal feeds ${t} Payroll Sync; Payroll Sync is fed by ${t} Portal.`);
  await expectNoViolations(admin, "Add relationship type dialog");
  await dialog.getByRole("button", { name: "Add relationship type" }).click();
  await expect(dialog).toBeHidden();
  await expect(add).toBeFocused();
  await expect(admin.getByText(`${name} added.`).first()).toBeVisible();
  await expect(row(name)).toContainText(inverse);
  await expect(row(name)).toContainText(slug);
  await expect(row(name)).toContainText("Added");
  await expect(row(name)).toContainText("Not used");
  // A name that makes the same slug is refused on the name.
  await add.click();
  await dialog.getByLabel("Name (required)").fill(name.toUpperCase());
  await dialog.getByRole("button", { name: "Add relationship type" }).click();
  await expect(dialog.getByRole("alert").first()).toContainText("already exists");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // Use it: A feeds B, from A's Relationships tab; the picker offers the added type both ways.
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await editor.goto(`/applications/${a.id}?tab=relationships`);
  await editor.getByRole("button", { name: "Add relationship" }).click();
  const link = editor.getByRole("dialog", { name: `Add relationship — ${t} A` });
  const picker = link.getByLabel("Relationship (required)");
  await expect(picker.locator("option", { hasText: `${t} A is fed by ${t}… (the other record feeds ${t} it)` })).toHaveCount(1);
  await picker.selectOption({ label: `${t} A feeds ${t}…` });
  await expect(link).toContainText("Pushes records downstream.");
  await link.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} B`);
  await link.getByRole("option", { name: new RegExp(`${t} B`) }).click();
  await link.getByRole("button", { name: "Add relationship" }).click();
  await expect(link).toBeHidden();
  const section = `${name} / is fed by ${t} table`;
  const fromA = editor.getByRole("region", { name: section }).getByRole("row", { name: new RegExp(`${t} B`) });
  await expect(fromA.getByRole("cell", { name, exact: true })).toBeVisible();

  // The other side reads the inverse name; editing from there keeps the type and the direction.
  await editor.goto(`/applications/${b.id}?tab=relationships`);
  const fromB = editor.getByRole("region", { name: section }).getByRole("row", { name: new RegExp(`${t} A`) });
  await expect(fromB.getByRole("cell", { name: inverse, exact: true })).toBeVisible();
  await expectNoViolations(editor, "Relationships tab with an added type");
  await fromB.getByRole("button", { name: `Edit link with ${t} A` }).click();
  const edit = editor.getByRole("dialog", { name: `Edit relationship — ${t} B` });
  await expect(edit.getByLabel("Relationship (required)")).toHaveValue(`${slug}|in`);
  await edit.getByLabel("Purpose").fill("Nightly push");
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(edit).toBeHidden();
  await expect(fromB).toContainText("Nightly push");
  const [saved] = await (await editor.request.get(`/api/v1/relationships?resource=${a.id}`)).json();
  expect(saved.type).toBe(slug);
  expect(saved.relationship_type.name).toBe(name);
  expect(saved.source.id).toBe(a.id);
  // The confirm dialog and history put the link in words from the source.
  await fromB.getByRole("button", { name: `Confirm link with ${t} A` }).click();
  await expect(editor.getByRole("dialog")).toContainText(`${t} A feeds ${t} ${t} B`);
  await editor.keyboard.press("Escape");

  // Delete in use: explained with the count and the links, no delete button; the API says the same (409).
  await admin.reload();
  await expect(row(name)).toContainText("1 relationship");
  await table.getByRole("button", { name: `Delete ${name}` }).click();
  const del = admin.getByRole("dialog", { name: `Delete ${name}?` });
  await expect(del.getByTestId("delete-consequences")).toContainText(`${name} is in use, so it can't be deleted: 1 relationship uses it.`);
  await expect(del.getByRole("link", { name: `${t} A feeds ${t} ${t} B` })).toHaveAttribute("href", `/applications/${a.id}?tab=relationships`);
  await expect(del.getByRole("button", { name: `Delete ${name}` })).toHaveCount(0);
  await expectNoViolations(admin, "Delete relationship type, in use");
  await del.getByRole("button", { name: "Cancel" }).click();
  await expect(table.getByRole("button", { name: `Delete ${name}` })).toBeFocused();
  const types: { id: string; slug: string }[] = await (await admin.request.get("/api/v1/relationship-types")).json();
  const added = types.find((x) => x.slug === slug)!;
  const refused = await apiStatus(admin, `relationship-types/${added.id}`, "DELETE");
  expect(refused.status).toBe(409);
  expect(refused.body.blocking_references.relationships).toBe(1);

  // Remove the link, then the delete goes through.
  await editor.reload();
  await fromB.getByRole("button", { name: `Remove link with ${t} A` }).click();
  const remove = editor.getByRole("dialog", { name: "Remove this relationship?" });
  await expect(remove).toContainText(`${t} A feeds ${t} ${t} B`);
  await remove.getByRole("button", { name: "Remove relationship" }).click();
  await expect(editor.getByText("Relationship removed.").first()).toBeVisible();
  await expect(editor.getByRole("region", { name: section })).toHaveCount(0); // an added type's section goes with its last link

  await admin.reload();
  await expect(row(name)).toContainText("Not used");
  await table.getByRole("button", { name: `Delete ${name}` }).click();
  await expect(del).toContainText(`No relationship uses ${name}.`);
  await del.getByRole("button", { name: `Delete ${name}` }).click();
  await expect(del).toBeHidden();
  await expect(admin.getByText(`${name} deleted.`).first()).toBeVisible();
  await expect(row(name)).toHaveCount(0);
});

test("The in-use delete dialog lists two or more links, each a target of at least 24 px (WCAG 2.2 2.5.8), in both themes", async ({ browser, baseURL }) => {
  // Short names, so each link sits on one line as in the QA report (a wrapped link is two lines high and hides the defect).
  const t = token().slice(-7);
  const name = `Syncs ${t}`;
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const added = await post(admin, "relationship-types", { name });
  const a = await post(admin, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(admin, "applications", { name: `${t} B`, lifecycle: "active" });
  const c = await post(admin, "applications", { name: `${t} C`, lifecycle: "active" });
  const links = [
    await post(admin, "relationships", { type: added.slug, source: a.id, target: b.id }),
    await post(admin, "relationships", { type: added.slug, source: a.id, target: c.id }),
  ];

  await admin.goto(`/relationship-types?q=${encodeURIComponent(name)}`);
  const table = admin.getByRole("region", { name: "Relationship types table" });
  await table.getByRole("button", { name: `Delete ${name}` }).click();
  const del = admin.getByRole("dialog", { name: `Delete ${name}?` });
  const used = del.getByRole("list", { name: `Links that use ${name}` }).getByRole("link");
  await expect(used).toHaveCount(2);
  // Every line box of every link, as axe measures it: one line each, at least 24 px high.
  const heights = await used.evaluateAll((els) => els.map((el) => Array.from(el.getClientRects(), (r) => r.height)));
  expect(heights.map((h) => h.length)).toEqual([1, 1]);
  for (const h of heights.flat()) expect(h).toBeGreaterThanOrEqual(24);
  await expectNoViolations(admin, "Delete relationship type, in use by two links"); // light and dark, wcag22aa (target-size)
  await del.getByRole("button", { name: "Cancel" }).click();

  // Leave nothing behind: the links, then the type.
  for (const l of links) await post(admin, `relationships/${l.id}`, undefined, "DELETE");
  await post(admin, `relationship-types/${added.id}`, undefined, "DELETE");
});

test("Core types: labels editable, slug and dependency fixed, delete refused; search and filters live in the URL", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/relationship-types");
  const table = admin.getByRole("region", { name: "Relationship types table" });
  await table.getByRole("button", { name: "Edit Depends on" }).click();
  const dialog = admin.getByRole("dialog", { name: "Edit Depends on" });
  await expect(dialog.getByLabel("Slug")).toHaveValue("depends_on");
  await expect(dialog.getByLabel("Slug")).toHaveAttribute("readonly", "");
  await expect(dialog.getByLabel("Counts as a dependency")).toHaveValue("Yes");
  await expect(dialog.getByLabel("Counts as a dependency")).toHaveAttribute("readonly", "");
  await expect(dialog).toContainText("Fixed for core types");
  await expectNoViolations(admin, "Edit core relationship type dialog");
  // Saving without a change changes nothing (core labels are shared with the other specs, so they stay as they are).
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(admin.getByText("No changes to Depends on.").first()).toBeVisible();

  const types: { id: string; slug: string }[] = await (await admin.request.get("/api/v1/relationship-types")).json();
  const core = types.find((x) => x.slug === "depends_on")!;
  const refused = await apiStatus(admin, `relationship-types/${core.id}`, "DELETE");
  expect(refused.status).toBe(409);
  expect(refused.body.type).toBe("/problems/core-relationship-type");

  await admin.getByLabel("Search relationship types").fill("replaced");
  await expect(admin).toHaveURL(/q=replaced/);
  await expect(table.getByRole("rowheader")).toHaveText([/^Replaces/]);
  await admin.goto("/relationship-types?dependency=yes&core=core");
  await expect(table.getByRole("rowheader")).toHaveText([/^Depends on/]);
  await admin.goto("/relationship-types?q=nothing-matches-this");
  await expect(admin.getByText("No relationship types match these filters")).toBeVisible();
  await admin.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(admin).toHaveURL(/\/relationship-types$/);
});

test("A 409 on delete that isn't about links in use shows the API's words, not an in-use count", async ({ browser, baseURL }) => {
  const t = token();
  const name = `Mirrors ${t}`;
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const added = await post(admin, "relationship-types", { name });
  // Another 409 (say, a conflicting change): no blocking_references, so the type isn't "in use".
  await admin.route(`**/api/v1/relationship-types/${added.id}`, (route) => route.request().method() === "DELETE"
    ? route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ type: "/problems/conflict", title: "Conflict", status: 409, detail: "Someone changed this type a moment ago. Reload and try again." }) })
    : route.continue());
  await admin.goto(`/relationship-types?q=${encodeURIComponent(name)}`);
  const table = admin.getByRole("region", { name: "Relationship types table" });
  await table.getByRole("button", { name: `Delete ${name}` }).click();
  const del = admin.getByRole("dialog", { name: `Delete ${name}?` });
  await del.getByRole("button", { name: `Delete ${name}` }).click();
  await expect(del.getByRole("alert")).toHaveText("Someone changed this type a moment ago. Reload and try again. Nothing was deleted.");
  await expect(del.getByTestId("delete-consequences")).toHaveCount(0);
  await expect(del.getByRole("button", { name: `Delete ${name}` })).toBeVisible(); // still offered: nothing uses it
  await admin.unroute(`**/api/v1/relationship-types/${added.id}`);
  await del.getByRole("button", { name: `Delete ${name}` }).click();
  await expect(del).toBeHidden();
  await expect(admin.getByText(`${name} deleted.`).first()).toBeVisible();
});

test("Editors and Viewers read relationship types without changing them", async ({ browser, baseURL }) => {
  for (const email of ["viewer@example.test", "editor@example.test"]) {
    const page = await pageAs(browser, baseURL, email);
    await page.goto("/relationship-types");
    await expect(page.getByRole("heading", { level: 1, name: "Relationship types" })).toBeVisible();
    await expect(page.getByText("You can view relationship types. Admins add, change and delete them.")).toBeVisible();
    const table = page.getByRole("region", { name: "Relationship types table" });
    await expect(table.getByRole("row", { name: /^Depends on/ })).toContainText("Is needed by");
    await expect(page.getByRole("button", { name: "Add relationship type" })).toHaveCount(0);
    await expect(table.getByRole("button")).toHaveCount(0);
    if (email.startsWith("viewer")) await expectNoViolations(page, "Relationship types, Viewer");
    await page.context().close();
  }
});

test("Integration fields: edit with labeled choices and a monitoring URL, read them on the detail page, filter the list in the URL", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const a = await post(page, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(page, "applications", { name: `${t} B`, lifecycle: "active" });
  const keyed = await post(page, "integrations", { name: `${t} Keyed`, lifecycle: "active", source: a.id, target: b.id });
  await post(page, "integrations", { name: `${t} OAuth`, lifecycle: "active", source: b.id, target: a.id, auth_method: "oauth", direction: "one_way" });
  await post(page, "integrations", { name: `${t} Unrecorded`, lifecycle: "active", source: a.id, target: b.id });

  // The detail page shows a dash for what isn't recorded.
  await page.goto(`/integrations/${keyed.id}`);
  const flow = page.locator('section[aria-labelledby="h-flow"]');
  await expect(flow.getByText("Direction", { exact: true }).locator("xpath=following-sibling::dd[1]")).toHaveText("—");
  await expect(flow.getByText("Monitoring", { exact: true }).locator("xpath=following-sibling::dd[1]")).toHaveText("—");

  await page.goto(`/integrations/${keyed.id}/edit`);
  await expect(page.getByLabel("Direction")).toHaveValue("");
  await expect(page.getByLabel("Direction").locator("option").first()).toHaveText("Not recorded");
  await page.getByLabel("Direction").selectOption({ label: "Both ways" });
  await page.getByLabel("Authentication").selectOption({ label: "API key" });
  await page.getByLabel("Data classification").selectOption({ label: "Confidential" });
  await page.getByLabel("Monitoring URL").fill("not a web address");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByLabel("Monitoring URL")).toBeFocused(); // the API's field error, focused
  await expect(page.getByLabel("Monitoring URL")).toHaveAttribute("aria-invalid", "true");
  await expectNoViolations(page, "Integration form with the extra fields and an error");
  await page.getByLabel("Monitoring URL").fill("https://status.example.test/runs/payroll");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(new RegExp(`/integrations/${keyed.id}$`));
  await expect(flow).toContainText("Both ways");
  await expect(flow).toContainText("API key");
  await expect(flow).toContainText("Confidential");
  const monitoring = flow.getByRole("link", { name: /status\.example\.test\/runs\/payroll/ });
  await expect(monitoring).toHaveAttribute("href", "https://status.example.test/runs/payroll");
  await expect(monitoring).toHaveAttribute("target", "_blank");
  await expectNoViolations(page, "Integration detail with the extra fields");

  // List: the authentication column, a two-way arrow in words, and choice filters kept in the URL.
  await page.goto(`/integrations?q=${encodeURIComponent(t)}`);
  const table = page.getByRole("region", { name: "Integrations table" });
  await expect(table.getByRole("row", { name: new RegExp(`${t} Keyed`) })).toContainText("API key");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Keyed`) })).toContainText("both ways with");
  // Rows by name, not a count: the search treats its last word as a prefix, so a parallel spec's token can match too.
  const shown = (n: string) => table.getByRole("rowheader", { name: `${t} ${n}`, exact: true });
  await expect(shown("Unrecorded")).toBeVisible();
  await page.getByRole("button", { name: "Add filter" }).click();
  await page.getByRole("menuitem", { name: "Authentication" }).click();
  const auth = page.getByRole("group", { name: "Authentication" });
  await auth.getByRole("button", { name: "API key" }).click();
  await expect(page).toHaveURL(/auth_method=api_key/);
  await expect(shown("OAuth")).toHaveCount(0);
  await expect(shown("Unrecorded")).toHaveCount(0);
  await expect(shown("Keyed")).toBeVisible();
  await auth.getByRole("button", { name: "OAuth 2.0" }).click();
  await expect(page).toHaveURL(/auth_method=oauth%2Capi_key|auth_method=oauth,api_key/);
  await expect(shown("OAuth")).toBeVisible();
  await expect(shown("Keyed")).toBeVisible();
  await expect(shown("Unrecorded")).toHaveCount(0);
  await expectNoViolations(page, "Integrations list with a choice filter");
  // The filter survives a reload; another one narrows it further.
  await page.goto(`/integrations?q=${encodeURIComponent(t)}&auth_method=oauth,api_key&data_classification=confidential`);
  await expect(shown("Keyed")).toBeVisible();
  await expect(shown("OAuth")).toHaveCount(0);
  await expect(page.getByRole("group", { name: "Data classification" }).getByRole("button", { name: "Confidential" })).toHaveAttribute("aria-pressed", "true");
  // Two-way and OAuth: none of this test's integrations. Checked by row name once the list has answered (the empty state
  // or a table): the search's trigram fallback can match another spec's near-identical token.
  await page.goto(`/integrations?q=${encodeURIComponent(t)}&direction=two_way&auth_method=oauth`);
  await expect(page.getByText("No integrations match these filters").or(table)).toBeVisible();
  for (const n of ["Keyed", "OAuth", "Unrecorded"]) await expect(shown(n)).toHaveCount(0);
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/integrations$/);
});
