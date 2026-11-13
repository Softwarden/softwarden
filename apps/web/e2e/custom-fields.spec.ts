import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Custom fields. The admin page (/custom-fields), a typed input per field in the record, integration
 *  and vendor forms, the API's field errors inline, the detail panel, optional table columns, cf_ filters in the URL,
 *  saved-filter problems, and the AI agent acceptance check path (a "Business case" value whose edit shows in Audit activity).
 *
 *  Custom fields are organization-wide, and a required one would break every other spec's record forms, so these run in
 *  their own organization, made by the Playwright-only `e2e/custom-fields-org` route. The tests of this file run one
 *  after another (one worker per file) and each deletes every field it made. */

const ADMIN = "cf-admin@example.test";
const EDITOR = "cf-editor@example.test";
const VIEWER = "cf-viewer@example.test";

let made: Promise<void> | null = null;
async function setup(browser: Browser, baseURL: string | undefined) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/custom-fields-org", {});
    await contoso.context().close();
  })();
  await made;
}

type Field = { id: string; name: string; label: string };
async function field(admin: Page, body: Record<string, unknown>): Promise<Field> {
  return post(admin, "custom-fields", { applies_to: ["application"], ...body });
}

test.afterEach(async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const fields: Field[] = await (await admin.request.get("/api/v1/custom-fields")).json();
  for (const f of fields) await post(admin, `custom-fields/${f.id}`, undefined, "DELETE");
  await admin.context().close();
});

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

test("Custom fields page: an Admin adds a field of every type, edits one, meets the locked type, choice and kind rules, and deletes with a count", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const t = token();
  await admin.goto("/custom-fields");
  await expect(admin.getByRole("heading", { level: 1, name: "Custom fields" })).toBeVisible();
  await expect(admin.getByRole("link", { name: "Custom fields" })).toBeVisible(); // in the sidebar's Admin section
  await expect(admin.getByText("No custom fields yet")).toBeVisible();
  await expectNoViolations(admin, "Custom fields page, empty");

  const types: [string, string][] = [
    ["Text (one line)", "text"], ["Long text", "longtext"], ["Whole number", "integer"], ["Decimal number", "decimal"], ["Yes / no", "boolean"],
    ["Date", "date"], ["Web address (URL)", "url"], ["Choice (one)", "select"], ["Choices (any number)", "multiselect"], ["JSON object", "object"],
  ];
  const table = admin.getByRole("region", { name: "Custom fields table" });
  const row = (label: string) => table.getByRole("row", { name: new RegExp(label) });
  for (const [i, [typeLabel, type]] of types.entries()) {
    await admin.getByRole("button", { name: "Add custom field" }).first().click();
    const dialog = admin.getByRole("dialog", { name: "Add custom field" });
    await dialog.getByLabel("Label (required)").fill(`${t} ${type} field`);
    // The name follows the label until it is typed in.
    await expect(dialog.getByLabel("Name (required)")).toHaveValue(slug(`${t} ${type} field`));
    await dialog.getByLabel("Type (required)").selectOption({ label: typeLabel });
    if (type === "select" || type === "multiselect") await dialog.getByLabel("Choices (required)").fill("Gold\nSilver\nBronze");
    await dialog.getByRole("group", { name: "Applies to (required)" }).getByRole("checkbox", { name: "Applications" }).check();
    if (type === "integer") await dialog.getByLabel("Default", { exact: true }).fill("5");
    if (type === "boolean") await dialog.getByLabel("Default", { exact: true }).selectOption({ label: "Yes" });
    if (type === "select") await dialog.getByLabel("Default", { exact: true }).selectOption("Silver");
    if (type === "multiselect") await dialog.getByRole("group", { name: "Default" }).getByRole("checkbox", { name: "Bronze" }).check();
    if (type === "date") await dialog.getByLabel("Reminder source").check();
    if (type === "text") await dialog.getByLabel("Description").fill("Shown under the field in forms.");
    await dialog.getByLabel("Weight").fill(String(10 + i));
    if (i === 0) await expectNoViolations(admin, "Add custom field dialog, filled");
    await dialog.getByRole("button", { name: "Add custom field" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(row(`${t} ${type} field`)).toContainText(typeLabel);
  }
  await expect(admin.getByRole("status").filter({ hasText: `${t} object field added.` })).toBeAttached();
  await expect(row(`${t} integer field`)).toContainText("5");
  await expect(row(`${t} select field`)).toContainText("Silver");
  await expect(row(`${t} multiselect field`)).toContainText("Bronze");
  await expect(row(`${t} date field`)).toContainText("Reminder source");
  await expect(row(`${t} text field`)).toContainText("Shown under the field in forms.");
  // Weight order: text (10) first, object (19) last.
  await expect(table.getByRole("rowheader").first()).toContainText(`${t} text field`);
  await expect(table.getByRole("rowheader").last()).toContainText(`${t} object field`);
  await expectNoViolations(admin, "Custom fields page with a field of every type");

  // The API's field errors are shown inline and focus moves to the first one: the same label and name again.
  await admin.getByRole("button", { name: "Add custom field" }).click();
  let dialog = admin.getByRole("dialog", { name: "Add custom field" });
  await dialog.getByLabel("Label (required)").fill(`${t} text field`);
  await dialog.getByRole("group", { name: "Applies to (required)" }).getByRole("checkbox", { name: "Components" }).check();
  await dialog.getByRole("button", { name: "Add custom field" }).click();
  await expect(dialog.getByLabel("Label (required)")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByLabel("Label (required)")).toBeFocused();
  await expect(dialog.getByLabel("Name (required)")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByRole("alert").filter({ hasText: "Fix these fields" })).toContainText("already exists");
  await expectNoViolations(admin, "Add custom field dialog with the API's field errors");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(admin.getByRole("button", { name: "Add custom field" })).toBeFocused();

  // Edit: the label changes; the name is read only.
  await admin.getByRole("button", { name: `Edit ${t} text field` }).click();
  dialog = admin.getByRole("dialog", { name: `Edit ${t} text field` });
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveAttribute("readonly", "");
  await expect(dialog.getByLabel("Name", { exact: true })).toHaveValue(slug(`${t} text field`));
  await dialog.getByLabel("Label (required)").fill(`${t} Renamed text`);
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(row(`${t} Renamed text`)).toBeVisible();
  await expect(admin.getByRole("button", { name: `Edit ${t} Renamed text` })).toBeFocused();

  // A record holds values: the type is locked, and removing a choice or a kind it uses is refused with a count.
  const p = slug(t);
  await post(admin, "applications", { name: `${t} Alpha`, lifecycle: "active", custom_field_data: { [`${p}_select_field`]: "Gold", [`${p}_integer_field`]: 7 } });
  await admin.reload();
  await expect(row(`${t} integer field`)).toContainText("1 record");
  await admin.getByRole("button", { name: `Edit ${t} integer field` }).click();
  dialog = admin.getByRole("dialog", { name: `Edit ${t} integer field` });
  await expect(dialog.getByLabel("Type", { exact: true })).toHaveAttribute("readonly", "");
  await expect(dialog.getByLabel("Type", { exact: true })).toHaveValue("Whole number");
  await expect(dialog).toContainText("Locked: 1 record holds a value");
  await expectNoViolations(admin, "Edit dialog with the type locked");
  // Saving untouched sends nothing: the default 5 as typed is the stored 5.
  const patches: string[] = [];
  admin.on("request", (r) => { if (r.method() === "PATCH" && r.url().includes("/api/v1/custom-fields/")) patches.push(r.url()); });
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(admin.getByRole("status").filter({ hasText: `No changes to ${t} integer field.` })).toBeAttached();
  expect(patches).toEqual([]);

  await admin.getByRole("button", { name: `Edit ${t} select field` }).click();
  dialog = admin.getByRole("dialog", { name: `Edit ${t} select field` });
  await expect(dialog.getByLabel("Choices (required)")).toHaveAccessibleDescription(/removing one in use is refused/);
  await dialog.getByLabel("Choices (required)").fill("Silver\nBronze");
  await dialog.getByRole("group", { name: "Applies to (required)" }).getByRole("checkbox", { name: "Applications" }).uncheck();
  await dialog.getByRole("group", { name: "Applies to (required)" }).getByRole("checkbox", { name: "Components" }).check();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog.getByLabel("Choices (required)")).toBeFocused();
  await expect(dialog.getByLabel("Choices (required)")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByLabel("Choices (required)")).toHaveAccessibleDescription(/^Still in use: Gold \(1 record\)/);
  await expect(dialog.getByRole("group", { name: "Applies to (required)" })).toContainText("Values exist for 1 of the applications");
  await expectNoViolations(admin, "Edit dialog with the choice and kind refusals");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // Delete with a count: a checkbox confirms that the values go.
  await admin.getByRole("button", { name: `Delete ${t} select field` }).click();
  dialog = admin.getByRole("dialog", { name: `Delete ${t} select field?` });
  await expect(dialog.getByTestId("delete-consequences")).toContainText("1 record holds a value");
  await expect(dialog.getByTestId("delete-consequences")).toContainText("Saved filters that use");
  await expectNoViolations(admin, "Delete custom field dialog with a count");
  await dialog.getByRole("button", { name: `Delete ${t} select field` }).click();
  await expect(dialog.getByText("Tick the box to confirm")).toBeVisible();
  await expect(dialog.getByRole("checkbox", { name: /Remove the .* values from 1 record/ })).toBeFocused();
  await dialog.getByRole("checkbox", { name: /Remove the .* values from 1 record/ }).check();
  await dialog.getByRole("button", { name: `Delete ${t} select field` }).click();
  await expect(dialog).toHaveCount(0);
  await expect(admin.getByRole("status").filter({ hasText: "1 record no longer holds a value" })).toBeAttached();
  await expect(row(`${t} select field`)).toHaveCount(0);
  // The Delete button that opened the dialog is gone with its row: focus is on the page heading, not lost.
  await expect(admin.getByRole("heading", { level: 1, name: "Custom fields" })).toBeFocused();
  const app = (await (await admin.request.get(`/api/v1/applications?q=${t}`)).json()).results[0];
  expect(Object.keys(app.custom_field_data)).not.toContain(`${p}_select_field`);

  // No values: nothing to confirm.
  await admin.getByRole("button", { name: `Delete ${t} url field` }).click();
  dialog = admin.getByRole("dialog", { name: `Delete ${t} url field?` });
  await expect(dialog.getByTestId("delete-consequences")).toContainText("No record holds a value");
  await dialog.getByRole("button", { name: `Delete ${t} url field` }).click();
  await expect(row(`${t} url field`)).toHaveCount(0);

  // Filters in the URL.
  await admin.goto("/custom-fields?type=date");
  await expect(table.getByRole("rowheader")).toHaveCount(1);
  await expect(admin.getByLabel("Type", { exact: true })).toHaveValue("date");
  await admin.goto("/custom-fields?applies_to=vendor");
  await expect(admin.getByText("No custom fields match these filters")).toBeVisible();
  await admin.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(admin).toHaveURL(/\/custom-fields$/);

  // A Viewer reads the page: no Add, Edit or Delete, and no sidebar entry (it is an Admin page).
  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto("/custom-fields");
  await expect(viewer.getByText("You can view custom fields. Admins add, change and delete them.")).toBeVisible();
  await expect(viewer.getByRole("region", { name: "Custom fields table" }).getByRole("rowheader", { name: new RegExp(`${t} date field`) })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Add custom field" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Edit / })).toHaveCount(0);
  await expect(viewer.getByRole("navigation").getByRole("link", { name: "Custom fields" })).toHaveCount(0);
  await expectNoViolations(viewer, "Custom fields page for a Viewer");
});

test("Record form: a typed input per field with defaults; the detail panel formats each value; an edit sends only what changed", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const t = token();
  const p = slug(t);
  const kinds = ["application", "component"];
  await field(admin, { name: `${p}_note`, label: `${t} Note`, type: "text", weight: 1, applies_to: kinds, description: "One line." });
  await field(admin, { name: `${p}_details`, label: `${t} Details`, type: "longtext", weight: 2, applies_to: kinds });
  await field(admin, { name: `${p}_seats`, label: `${t} Seats`, type: "integer", weight: 3, applies_to: kinds, default: 5 });
  await field(admin, { name: `${p}_cost`, label: `${t} Cost`, type: "decimal", weight: 4, applies_to: kinds });
  await field(admin, { name: `${p}_flag`, label: `${t} Flag`, type: "boolean", weight: 5, applies_to: kinds });
  await field(admin, { name: `${p}_expiry`, label: `${t} Expiry`, type: "date", weight: 6, applies_to: kinds });
  await field(admin, { name: `${p}_site`, label: `${t} Site`, type: "url", weight: 7, applies_to: kinds });
  await field(admin, { name: `${p}_tier`, label: `${t} Tier`, type: "select", choices: ["Gold", "Silver"], weight: 8, applies_to: kinds });
  await field(admin, { name: `${p}_regions`, label: `${t} Regions`, type: "multiselect", choices: ["EU", "US", "APAC"], weight: 9, applies_to: kinds });
  await field(admin, { name: `${p}_meta`, label: `${t} Meta`, type: "object", weight: 10, applies_to: kinds });

  const page = await pageAs(browser, baseURL, EDITOR);
  await page.goto("/applications/new");
  await expect(page.getByRole("heading", { level: 2, name: "Custom fields" })).toBeVisible();
  await expect(page.getByLabel(`${t} Seats`)).toHaveValue("5"); // the default, pre-filled
  await expect(page.getByLabel(`${t} Note`)).toHaveAccessibleDescription("One line.");
  await expect(page.getByLabel(`${t} Flag`)).toHaveValue("");
  await page.getByLabel("Name (required)").fill(`${t} Ledger`);
  await page.getByLabel(`${t} Details`).fill("Line one\nLine two");
  await page.getByLabel(`${t} Seats`).fill("12500");
  await page.getByLabel(`${t} Cost`).fill("12.5");
  await page.getByLabel(`${t} Flag`).selectOption({ label: "Yes" });
  await page.getByLabel(`${t} Expiry`).fill("2027-03-01");
  await page.getByLabel(`${t} Site`).fill("https://ledger.example/docs");
  await page.getByLabel(`${t} Tier`).selectOption("Gold");
  const regions = page.getByRole("group", { name: `${t} Regions` });
  await regions.getByRole("checkbox", { name: "EU" }).check();
  await regions.getByRole("checkbox", { name: "APAC" }).check();
  await page.getByLabel(`${t} Meta`).fill('{"owner": "finance", "tier": 2}');
  // The kind switch keeps what was typed; components have the same fields here.
  await page.getByRole("radio", { name: "Component" }).check();
  await expect(page.getByLabel(`${t} Seats`)).toHaveValue("12500");
  await page.getByRole("radio", { name: "Application" }).check();
  await expectNoViolations(page, "Application form with custom fields");
  const [created] = await Promise.all([
    page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/v1/applications")),
    page.getByRole("button", { name: "Save" }).click(),
  ]);
  expect(created.postDataJSON().custom_field_data).toEqual({
    [`${p}_details`]: "Line one\nLine two", [`${p}_seats`]: "12500", [`${p}_cost`]: "12.5", [`${p}_flag`]: true, [`${p}_expiry`]: "2027-03-01",
    [`${p}_site`]: "https://ledger.example/docs", [`${p}_tier`]: "Gold", [`${p}_regions`]: ["EU", "APAC"], [`${p}_meta`]: { owner: "finance", tier: 2 },
  });
  await expect(page).toHaveURL(/\/applications\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;

  const panel = page.getByRole("region", { name: "Custom fields" });
  const value = (label: string) => panel.getByRole("definition").nth(["Note", "Details", "Seats", "Cost", "Flag", "Expiry", "Site", "Tier", "Regions", "Meta"].indexOf(label));
  await expect(panel.getByRole("term").first()).toHaveText(`${t} Note`);
  await expect(value("Note")).toHaveText("Not set");
  await expect(value("Details")).toHaveText("Line one\nLine two");
  await expect(value("Seats")).toHaveText("12,500");
  await expect(value("Cost")).toHaveText("12.5");
  await expect(value("Flag")).toHaveText("Yes");
  await expect(value("Expiry")).toHaveText("2027-03-01");
  await expect(value("Site").getByRole("link", { name: /https:\/\/ledger.example\/docs/ })).toHaveAttribute("href", "https://ledger.example/docs");
  await expect(value("Tier")).toHaveText("Gold");
  await expect(value("Regions").getByRole("list", { name: `${t} Regions` })).toHaveText(/EU\s*APAC/);
  await expect(value("Meta").locator("pre")).toContainText('"owner": "finance"');
  await expectNoViolations(page, "Application detail with the Custom fields panel");

  // Edit: the form starts from the stored values; only the changed name is sent.
  await page.getByRole("link", { name: `Edit ${t} Ledger` }).click();
  await expect(page.getByLabel(`${t} Seats`)).toHaveValue("12500");
  await expect(page.getByRole("group", { name: `${t} Regions` }).getByRole("checkbox", { name: "APAC" })).toBeChecked();
  await expect(page.getByLabel(`${t} Meta`)).toHaveValue(/"owner": "finance"/);
  await page.getByLabel(`${t} Note`).fill("Primary ledger");
  const [patch] = await Promise.all([
    page.waitForRequest((r) => r.method() === "PATCH" && r.url().endsWith(`/api/v1/applications/${id}`)),
    page.getByRole("button", { name: "Save" }).click(),
  ]);
  expect(patch.postDataJSON().custom_field_data).toEqual({ [`${p}_note`]: "Primary ledger" });
  await expect(page).toHaveURL(`/applications/${id}`);
  await expect(value("Note")).toHaveText("Primary ledger");

  // A Viewer sees the panel too.
  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto(`/applications/${id}`);
  await expect(viewer.getByRole("region", { name: "Custom fields" })).toContainText("Primary ledger");
});

test("Field errors: the API's are shown inline on the right input and focus the first; a JSON object is checked before sending", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const t = token();
  const p = slug(t);
  await field(admin, { name: `${p}_seats`, label: `${t} Seats`, type: "integer", weight: 1 });
  await field(admin, { name: `${p}_owner`, label: `${t} Cost owner`, type: "text", required: true, weight: 2 });
  await field(admin, { name: `${p}_meta`, label: `${t} Meta`, type: "object", weight: 3 });

  const page = await pageAs(browser, baseURL, EDITOR);
  const posts: string[] = [];
  page.on("request", (r) => { if (r.method() === "POST" && r.url().endsWith("/api/v1/applications")) posts.push(r.url()); });
  await page.goto("/applications/new");
  await expect(page.getByLabel(`${t} Cost owner (required)`)).toBeVisible();
  await page.getByLabel("Name (required)").fill(`${t} Box`);
  await page.getByLabel(`${t} Seats`).fill("abc");
  await page.getByLabel(`${t} Meta`).fill("{bad");
  await page.getByLabel(`${t} Meta`).blur();
  await expect(page.getByLabel(`${t} Meta`)).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByLabel(`${t} Meta`)).toHaveAccessibleDescription(/This isn't valid JSON/);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByLabel(`${t} Meta`)).toBeFocused();
  expect(posts).toEqual([]); // refused before the API
  await page.getByLabel(`${t} Meta`).fill('{"a": 1}');

  await page.getByRole("button", { name: "Save" }).click();
  const seats = page.getByLabel(`${t} Seats`);
  const owner = page.getByLabel(`${t} Cost owner (required)`);
  await expect(seats).toHaveAttribute("aria-invalid", "true");
  await expect(seats).toHaveAccessibleDescription(/^Enter a whole number\./);
  await expect(owner).toHaveAttribute("aria-invalid", "true");
  await expect(owner).toHaveAccessibleDescription(`${t} Cost owner is required.`);
  await expect(page.getByLabel(`${t} Meta`)).not.toHaveAttribute("aria-invalid", "true");
  await expect(seats).toBeFocused(); // the first invalid field, in form order
  const summary = page.getByRole("alert").filter({ hasText: "Fix these fields" });
  await expect(summary).toContainText("Enter a whole number.");
  await expect(summary).toContainText(`${t} Cost owner is required.`);
  await expect(page.getByLabel("Name (required)")).toHaveValue(`${t} Box`); // nothing typed is lost
  await expectNoViolations(page, "Record form with custom field errors");
  // The summary's links go to the field.
  await summary.getByRole("link", { name: `${t} Cost owner is required.` }).click();
  await expect(owner).toBeFocused();

  await seats.fill("3");
  await owner.fill("Finance");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/applications\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  await expect(page.getByRole("region", { name: "Custom fields" })).toContainText("Finance");

  // Clearing a required value on edit is refused on that field.
  await page.goto(`/applications/${id}/edit`);
  await page.getByLabel(`${t} Cost owner (required)`).fill("");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByLabel(`${t} Cost owner (required)`)).toBeFocused();
  await expect(page.getByLabel(`${t} Cost owner (required)`)).toHaveAccessibleDescription(`${t} Cost owner is required.`);
  await expect(page).toHaveURL(`/applications/${id}/edit`);
});

test("Lists: optional cf_ columns, each custom filter type in the URL, and a stale filter shown as an error with Clear filters", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const t = token();
  const p = slug(t);
  await field(admin, { name: `${p}_note`, label: `${t} Note`, type: "text", weight: 1 });
  await field(admin, { name: `${p}_tier`, label: `${t} Tier`, type: "select", choices: ["Gold", "Silver"], weight: 2 });
  await field(admin, { name: `${p}_regions`, label: `${t} Regions`, type: "multiselect", choices: ["EU", "US"], weight: 3 });
  await field(admin, { name: `${p}_flag`, label: `${t} Flag`, type: "boolean", weight: 4 });
  await field(admin, { name: `${p}_seats`, label: `${t} Seats`, type: "integer", weight: 5 });
  await field(admin, { name: `${p}_expiry`, label: `${t} Expiry`, type: "date", weight: 6 });
  await field(admin, { name: `${p}_meta`, label: `${t} Meta`, type: "object", weight: 7 });
  const v = (o: Record<string, unknown>) => Object.fromEntries(Object.entries(o).map(([k, x]) => [`${p}_${k}`, x]));
  await post(admin, "applications", { name: `${t} Alpha`, lifecycle: "active", custom_field_data: v({ note: "alpha notes", tier: "Gold", regions: ["EU"], flag: true, seats: 10, expiry: "2027-01-01", meta: { k: 1 } }) });
  await post(admin, "applications", { name: `${t} Bravo`, lifecycle: "active", custom_field_data: v({ note: "bravo", tier: "Silver", regions: ["US", "EU"], flag: false, seats: 50, expiry: "2028-06-01" }) });
  await post(admin, "applications", { name: `${t} Charlie`, lifecycle: "active" });

  const page = await pageAs(browser, baseURL, EDITOR);
  await page.goto(`/applications?q=${t}`);
  const table = page.getByRole("region", { name: "Applications table" });
  // This test's rows only (the search also matches other tests' similar tokens).
  const names = () => table.getByRole("rowheader").allInnerTexts().then((x) => x.map((s) => s.trim().split("\n")[0]).filter((n) => n.startsWith(`${t} `)).sort());
  await expect.poll(names).toEqual([`${t} Alpha`, `${t} Bravo`, `${t} Charlie`]);

  // Columns: off by default, one per field; values formatted.
  await expect(table.getByRole("columnheader", { name: `${t} Tier` })).toHaveCount(0);
  await page.getByRole("button", { name: /^Columns/ }).click();
  const menu = page.getByRole("menu", { name: "Show columns" });
  await expect(menu.getByRole("menuitemcheckbox", { name: `${t} Tier` })).toHaveAttribute("aria-checked", "false");
  await menu.getByRole("menuitemcheckbox", { name: `${t} Tier` }).click();
  await menu.getByRole("menuitemcheckbox", { name: `${t} Seats` }).click();
  await menu.getByRole("menuitemcheckbox", { name: `${t} Meta` }).click();
  await page.keyboard.press("Escape");
  await expect(table.getByRole("columnheader", { name: `${t} Tier` })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Alpha`) })).toContainText("Gold");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Bravo`) })).toContainText("50");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Alpha`) })).toContainText('{"k":1}');
  await expect(table.getByRole("row", { name: new RegExp(`${t} Charlie`) })).toContainText("Not set");
  await expectNoViolations(page, "Applications list with custom field columns");

  const addFilter = async (label: string) => {
    await page.getByRole("button", { name: "Add filter" }).click();
    await page.getByRole("menuitem", { name: label, exact: true }).click();
  };
  const remove = async (label: string) => page.getByRole("button", { name: `Remove ${label} filter` }).click();

  // Text: contains.
  await addFilter(`${t} Note contains`);
  await expect(page.getByLabel(`${t} Note contains`, { exact: true })).toBeFocused();
  await page.getByLabel(`${t} Note contains`, { exact: true }).fill("alpha");
  await expect(page).toHaveURL(new RegExp(`cf_${p}_note=alpha`));
  await expect.poll(names).toEqual([`${t} Alpha`]);
  await remove(`${t} Note contains`);
  await expect(page).not.toHaveURL(new RegExp(`cf_${p}_note=`));

  // Choices: chips, any of them.
  await addFilter(`${t} Tier`);
  const tier = page.getByRole("group", { name: `${t} Tier` });
  await expect(tier.getByRole("button", { name: "Gold" })).toBeFocused();
  await tier.getByRole("button", { name: "Gold" }).click();
  await expect(page).toHaveURL(new RegExp(`cf_${p}_tier=Gold(&|$)`));
  await expect.poll(names).toEqual([`${t} Alpha`]);
  await tier.getByRole("button", { name: "Silver" }).click();
  await expect(page).toHaveURL(new RegExp(`cf_${p}_tier=Gold%2CSilver`));
  await expect.poll(names).toEqual([`${t} Alpha`, `${t} Bravo`]);
  await page.reload();
  await expect(page.getByRole("group", { name: `${t} Tier` }).getByRole("button", { name: "Silver" })).toHaveAttribute("aria-pressed", "true");
  await expectNoViolations(page, "Applications list with a choice filter");
  await remove(`${t} Tier`);
  await addFilter(`${t} Regions`);
  await page.getByRole("group", { name: `${t} Regions` }).getByRole("button", { name: "US" }).click();
  await expect.poll(names).toEqual([`${t} Bravo`]);
  await remove(`${t} Regions`);

  // Yes / no.
  await addFilter(`${t} Flag`);
  await page.getByLabel(`${t} Flag`, { exact: true }).selectOption({ label: "No" });
  await expect(page).toHaveURL(new RegExp(`cf_${p}_flag=false`));
  await expect.poll(names).toEqual([`${t} Bravo`]);
  await remove(`${t} Flag`);

  // Numbers and dates: from and to.
  await addFilter(`${t} Seats from`);
  await page.getByLabel(`${t} Seats from`, { exact: true }).fill("20");
  await page.getByLabel(`${t} Seats from`, { exact: true }).press("Enter");
  await expect(page).toHaveURL(new RegExp(`cf_${p}_seats__gte=20`));
  await expect.poll(names).toEqual([`${t} Bravo`]);
  await addFilter(`${t} Expiry to`);
  await page.getByLabel(`${t} Expiry to`, { exact: true }).fill("2027-12-31");
  await expect(page).toHaveURL(new RegExp(`cf_${p}_expiry__lte=2027-12-31`));
  await expect(page.getByText("No applications match these filters")).toBeVisible();
  await expectNoViolations(page, "Applications list with range filters and no rows");
  await remove(`${t} Seats from`);
  await expect.poll(names).toEqual([`${t} Alpha`]);
  await remove(`${t} Expiry to`);

  // Is set / not set (all an object field takes).
  await addFilter(`${t} Meta is set`);
  await page.getByLabel(`${t} Meta is set`, { exact: true }).selectOption({ label: "No value" });
  await expect(page).toHaveURL(new RegExp(`cf_${p}_meta__set=false`));
  await expect.poll(names).toEqual([`${t} Bravo`, `${t} Charlie`]);
  await page.getByLabel(`${t} Meta is set`, { exact: true }).selectOption({ label: "Has a value" });
  await expect.poll(names).toEqual([`${t} Alpha`]);

  // A filter from a stale link: the list's 400 is shown with what it said and Clear filters; the chip can be removed.
  await page.goto(`/applications?q=${t}&cf_${p}_gone=x`);
  const problem = page.getByRole("alert").filter({ hasText: "Couldn't show applications with these filters" });
  await expect(problem).toContainText(`cf_${p}_gone`);
  await expect(problem).toContainText("This filter isn't available here.");
  await expect(page.getByLabel(`Unknown filter cf_${p}_gone`, { exact: true })).toHaveValue("x");
  await expectNoViolations(page, "Applications list refusing a stale custom field filter");
  await problem.getByRole("button", { name: "Clear filters" }).click();
  await expect(problem).toHaveCount(0);
  await expect(page).not.toHaveURL(/cf_/);
});

test("Saved filters: a filter the list would refuse shows its problems, and opens without the bad parameter", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const page = await pageAs(browser, baseURL, EDITOR);
  const t = token();
  // The API prunes a field's parameters from saved filters when it changes, so `problems` is a backstop that is hard to
  // reach through the API: the listing is extended here with one such filter.
  await page.route((url) => url.pathname === "/api/v1/saved-filters" && url.searchParams.get("kind") === "application", async (route) => {
    const response = await route.fetch();
    const list = await response.json();
    list.push({
      id: "00000000-0000-4000-8000-0000000000aa", name: `${t} Old tier`, kind: "application", query: { lifecycle: "active", cf_gone_tier: "Gold" },
      shared: false, builtin: false, mine: true, can_delete: true, problems: ["cf_gone_tier: This filter isn't available here."],
    });
    await route.fulfill({ response, json: list });
  });
  await page.goto("/applications");
  const saved = page.getByLabel("Saved views");
  await expect(saved.getByRole("option", { name: `${t} Old tier (needs fixing)` })).toBeAttached();
  await saved.selectOption({ label: `${t} Old tier (needs fixing)` });
  const problem = page.getByRole("alert").filter({ hasText: `“${t} Old tier” can't be opened as it was saved` });
  await expect(problem).toContainText("cf_gone_tier refers to a custom field that no longer exists or no longer applies here.");
  await expect(page).not.toHaveURL(/cf_gone_tier/); // nothing applied yet
  await expect(problem.getByRole("button", { name: `Delete “${t} Old tier”…` })).toBeVisible();
  await expectNoViolations(page, "Saved filter with problems");
  await problem.getByRole("button", { name: "Open without cf_gone_tier" }).click();
  await expect(page).toHaveURL(/lifecycle=active/);
  await expect(page).not.toHaveURL(/cf_gone_tier/);
  await expect(problem).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: `Opened “${t} Old tier” without cf_gone_tier` })).toBeAttached();
  await expect(page.getByLabel("Saved views")).toBeFocused();
  await expect(page.getByRole("region", { name: "Applications table" }).or(page.getByText("No applications match these filters"))).toBeVisible();
});

test("AI agent acceptance: an Editor creates a Copilot Studio agent with models, channels, owners and a Business case; the edit shows in Audit activity", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const t = token();
  await field(admin, { name: "business_case", label: "Business case", type: "longtext", applies_to: ["ai_agent"], description: "Why the organization runs this agent." });
  const page = await pageAs(browser, baseURL, EDITOR);
  const provider = await post(page, "ai-providers", { name: `${t} Labs` });
  const model = await post(page, "ai-models", { provider: provider.id, name: `${t} Alpha`, version: "1", modality: "text" });

  await page.goto("/ai-agents/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add AI agent" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(`${t} HR assistant`);
  await page.getByLabel("Platform", { exact: true }).selectOption({ label: "Copilot Studio" });
  await page.getByRole("group", { name: "Models" }).getByRole("checkbox", { name: new RegExp(`${t} Labs ${model.name}`) }).check();
  const channels = page.getByRole("group", { name: "Channels" });
  await channels.getByRole("checkbox", { name: "Teams" }).check();
  await channels.getByRole("checkbox", { name: "Microsoft 365 Copilot" }).check();
  await page.getByRole("combobox", { name: "Business owner" }).fill("Priya Raman");
  await page.getByRole("option", { name: /^Priya Raman/ }).first().click();
  await page.getByRole("combobox", { name: "Technical owner" }).fill("Dana Ortiz");
  await page.getByRole("option", { name: /^Dana Ortiz/ }).first().click();
  await expect(page.getByLabel("Business case")).toHaveAccessibleDescription("Why the organization runs this agent.");
  await page.getByLabel("Business case").fill("Answers HR policy questions in Teams.");
  await expectNoViolations(page, "AI agent form with a Business case");
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(/\/ai-agents\/[0-9a-f-]{36}$/);
  const id = page.url().split("/").pop()!;
  await expect(page.getByRole("region", { name: "Agent" })).toContainText("Copilot Studio");
  await expect(page.getByRole("region", { name: "Agent" }).getByRole("list", { name: "Channels" })).toHaveText(/Teams\s*Microsoft 365 Copilot/);
  await expect(page.getByRole("region", { name: "Ownership (confirmed fields)" })).toContainText("Priya Raman");
  await expect(page.getByRole("region", { name: "Custom fields" })).toContainText("Answers HR policy questions in Teams.");

  await page.getByRole("link", { name: `Edit ${t} HR assistant` }).click();
  await expect(page.getByLabel("Business case")).toHaveValue("Answers HR policy questions in Teams.");
  await page.getByLabel("Business case").fill("Answers HR policy questions in Teams and cuts tickets by a third.");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(`/ai-agents/${id}`);
  await expect(page.getByRole("region", { name: "Custom fields" })).toContainText("cuts tickets by a third");

  await page.getByRole("tab", { name: "Audit activity" }).click();
  const activity = page.getByRole("region", { name: "Audit activity table" });
  await activity.getByRole("row", { name: /Fields changed/ }).first().getByRole("button", { name: "Show changes" }).click();
  const diff = page.getByRole("table", { name: /^Changes in Fields changed/ });
  const row = diff.getByRole("row", { name: /^Business case/ });
  await expect(row.locator('[data-side="before"]')).toContainText("Answers HR policy questions in Teams.");
  await expect(row.locator('[data-side="after"]')).toContainText("cuts tickets by a third");
  await expectNoViolations(page, "AI agent Audit activity with the Business case diff");
});

test("Vendors and integrations: custom fields in their forms (with inline errors), the vendor table and the integration detail", async ({ browser, baseURL }) => {
  await setup(browser, baseURL);
  const admin = await pageAs(browser, baseURL, ADMIN);
  const t = token();
  const p = slug(t);
  await field(admin, { name: `${p}_account`, label: `${t} Account ID`, type: "text", applies_to: ["vendor"], weight: 1 });
  await field(admin, { name: `${p}_seats`, label: `${t} Licensed seats`, type: "integer", applies_to: ["vendor"], weight: 2 });
  await field(admin, { name: `${p}_method`, label: `${t} Transfer method`, type: "select", choices: ["SFTP", "API"], applies_to: ["integration"] });
  await field(admin, { name: `${p}_tier`, label: `${t} Vendor tier`, type: "select", choices: ["Strategic", "Tactical"], applies_to: ["vendor"], required: true, weight: 3 });

  const page = await pageAs(browser, baseURL, EDITOR);
  await page.goto("/admin/people?tab=vendors");
  const vendors = page.getByRole("region", { name: "Vendors table" });
  await expect(vendors.getByRole("columnheader", { name: "Custom fields" })).toBeVisible();
  await expect(vendors.getByRole("row", { name: /Zylker Software/ })).toContainText("None set");
  await vendors.getByRole("button", { name: "Edit Zylker Software" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit Zylker Software" });
  await dialog.getByLabel(`${t} Account ID`).fill("ZY-42");
  await dialog.getByLabel(`${t} Licensed seats`).fill("many");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog.getByLabel(`${t} Licensed seats`)).toBeFocused();
  await expect(dialog.getByLabel(`${t} Licensed seats`)).toHaveAccessibleDescription(/^Enter a whole number\./);
  await expectNoViolations(page, "Vendor dialog with a custom field error");
  await dialog.getByLabel(`${t} Licensed seats`).fill("25");
  const [patch] = await Promise.all([
    page.waitForRequest((r) => r.method() === "PATCH" && /\/api\/v1\/vendors\//.test(r.url())),
    dialog.getByRole("button", { name: "Save" }).click(),
  ]);
  expect(patch.postDataJSON().custom_field_data).toEqual({ [`${p}_account`]: "ZY-42", [`${p}_seats`]: "25" });
  await expect(dialog).toHaveCount(0);
  await expect(vendors.getByRole("row", { name: /Zylker Software/ })).toContainText(`${t} Account ID: ZY-42`);
  await expect(vendors.getByRole("row", { name: /Zylker Software/ })).toContainText(`${t} Licensed seats: 25`);
  await expectNoViolations(page, "Vendors table with custom fields");

  // "+ Add vendor" from the application form: a required vendor field opens the vendor dialog with the typed name.
  await page.goto("/applications/new");
  await page.getByLabel("Name (required)").fill(`${t} Ledger`);
  await page.getByRole("combobox", { name: "Vendor" }).fill(`${t} Acme`);
  await page.getByRole("option", { name: `+ Add vendor "${t} Acme"` }).click();
  const add = page.getByRole("dialog", { name: "Add vendor" });
  await expect(add.getByLabel("Name (required)")).toHaveValue(`${t} Acme`);
  await add.getByRole("button", { name: "Save" }).click();
  await expect(add.getByLabel(`${t} Vendor tier (required)`)).toBeFocused();
  await expect(add.getByLabel(`${t} Vendor tier (required)`)).toHaveAccessibleDescription(`${t} Vendor tier is required.`);
  await expectNoViolations(page, "Add vendor dialog from the application form with a required custom field");
  await add.getByLabel(`${t} Vendor tier (required)`).selectOption("Strategic");
  await add.getByRole("button", { name: "Save" }).click();
  await expect(add).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "Vendor" })).toHaveValue(`${t} Acme`);
  await expect(page.getByRole("combobox", { name: "Vendor" })).toBeFocused();
  await expect(page.getByRole("status").filter({ hasText: `Vendor ${t} Acme added and selected.` })).toBeVisible();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(/\/applications\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("region", { name: "Details" })).toContainText(`${t} Acme`);

  // The same from the renewal form.
  await page.goto("/renewals/new");
  await page.getByRole("combobox", { name: "Vendor" }).fill(`${t} Globex`);
  await page.getByRole("option", { name: `+ Add vendor "${t} Globex"` }).click();
  const addAgain = page.getByRole("dialog", { name: "Add vendor" });
  await addAgain.getByLabel(`${t} Vendor tier (required)`).selectOption("Tactical");
  await addAgain.getByRole("button", { name: "Save" }).click();
  await expect(addAgain).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "Vendor" })).toHaveValue(`${t} Globex`);

  // Integration: the form takes the field; the detail page shows it.
  const source = await post(page, "applications", { name: `${t} Source`, lifecycle: "active" });
  const target = await post(page, "applications", { name: `${t} Target`, lifecycle: "active" });
  const integration = await post(page, "integrations", { name: `${t} Feed`, lifecycle: "active", source: source.id, target: target.id });
  await page.goto(`/integrations/${integration.id}`);
  await expect(page.getByRole("region", { name: "Custom fields" })).toContainText("Not set");
  await page.goto(`/integrations/${integration.id}/edit`);
  await page.getByLabel(`${t} Transfer method`).selectOption("SFTP");
  await expectNoViolations(page, "Integration form with a custom field");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(`/integrations/${integration.id}`);
  const panel = page.getByRole("region", { name: "Custom fields" });
  await expect(panel).toContainText(`${t} Transfer method`);
  await expect(panel).toContainText("SFTP");
  await expectNoViolations(page, "Integration detail with the Custom fields panel");
});
