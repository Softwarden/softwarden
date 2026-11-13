import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Contracts. The list, the form with its live preview of the annualised cost and cancellation
 *  deadline, the contract page (lines with the allocation editor, the renewal it drives and its cycles, the chain of
 *  terms, History with its lines), the renewal page's "Dates come from the contract" and its refusal of those fields,
 *  the renew page (renew with defaults, cancel with notes, a newly required custom field fixed in place), a stale edit
 *  (412), the read-only Viewer, contracts in the palette, search and a record's Map, and axe in both themes.
 *
 *  Custom fields are organization-wide (a required one would break every contract form of the shared Contoso), so this
 *  file runs in its own organization (the Playwright-only `e2e/spec-org` route, `contracts`); its tests run in order. */

const ADMIN = "ct-admin@example.test";
const EDITOR = "ct-editor@example.test";
const VIEWER = "ct-viewer@example.test";

test.describe.configure({ mode: "serial" });

let made: Promise<void> | null = null;
async function setup(browser: Browser, baseURL: string | undefined) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "contracts" });
    await contoso.context().close();
  })();
  await made;
}
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  await setup(browser, baseURL);
  return pageAs(browser, baseURL, email);
}

/** YYYY-MM-DD `days` from today (UTC, the spec organisation's timezone). */
const day = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
const said = (page: Page, text: string) => expect(page.getByRole("status").filter({ hasText: text })).toHaveCount(1);
const app = (page: Page, name: string) => post(page, "applications", { name, lifecycle: "active" });
const contract = (page: Page, name: string, extra: Record<string, unknown> = {}) => post(page, "contracts", {
  name, amount: "1200.00", billing_frequency: "annual", start_date: day(-300), end_date: day(65), auto_renew: "yes", notice_value: 30, notice_unit: "days", ...extra,
});

let journey: { t: string; id: string; successor: string; renewal: string } | null = null;

test("Contract journey: add with a live preview, lines with fixed and shared shares, the renewal it drives, renew with defaults, History", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  await app(page, `${t} Ledger`);
  await app(page, `${t} Payroll`);
  await app(page, `${t} Timesheets`);

  // The form previews the annualised cost and the deadline as the values are typed (GET contracts/preview).
  await page.goto("/contracts/new");
  await expect(page.getByRole("heading", { level: 1, name: "Add contract" })).toBeVisible();
  await page.getByLabel("Name (required)").fill(`${t} Ledger suite`);
  await page.getByLabel("Amount").fill("1000.50");
  await page.getByLabel("Billing frequency").selectOption("monthly");
  await page.getByLabel("Start date").fill(day(-300));
  await page.getByLabel("End date").fill(day(65));
  await page.getByRole("radio", { name: "Yes", exact: true }).check();
  await page.getByLabel("Notice period, number").fill("30");
  const preview = page.getByTestId("contract-preview");
  await expect(preview).toContainText("Annualized cost: 12,006.00 USD");
  await expect(preview).toContainText(`Cancellation deadline: ${day(35)} (Calculated)`);
  await page.getByRole("radio", { name: "Create a renewal for this contract" }).check();
  await expectNoViolations(page, "Add contract form with its preview");
  await page.getByRole("button", { name: "Add contract" }).click();

  await expect(page.getByRole("heading", { level: 1, name: `${t} Ledger suite`, exact: true })).toBeVisible();
  // Shown on the page and said once, by the shell's announcer.
  await expect(page.getByRole("main").getByText("Contract added. Annualized cost 12,006.00 USD.")).toBeVisible();
  await expect(page.getByTestId("shell-announcer")).toContainText("Contract added. Annualized cost 12,006.00 USD.");
  await expect(page.getByTestId("drives-renewal")).toContainText(`Dates for renewal ${t} Ledger suite come from this contract`);
  const id = page.url().split("/contracts/")[1].split("?")[0];
  await expectNoViolations(page, "Contract page, overview");

  // Lines: a fixed share, a shared line, and a fixed share that would take the total over 100% (refused on the field).
  await page.getByRole("tab", { name: /^Lines/ }).click();
  await expect(page.getByRole("main").getByText("No lines yet")).toBeVisible();
  const addLine = async (name: string, percent: string | null) => {
    await page.getByRole("button", { name: "Add line" }).click();
    const dialog = page.getByRole("dialog", { name: "Add a line" });
    await dialog.getByRole("combobox", { name: "Record (required)" }).fill(name);
    await dialog.getByRole("option", { name: new RegExp(name) }).click();
    if (percent === null) await dialog.getByRole("radio", { name: /Shares the rest/ }).check();
    else await dialog.getByLabel("Fixed share (%)").fill(percent);
    return dialog;
  };
  let dialog = await addLine(`${t} Ledger`, "60");
  await expect(dialog.getByTestId("allocation-after")).toContainText("Fixed shares would total 60%, leaving 40% for shared lines (or Not allocated).");
  await dialog.getByRole("button", { name: "Add line" }).click();
  await expect(dialog).toHaveCount(0);
  await said(page, `${t} Ledger added with a fixed 60%.`);
  dialog = await addLine(`${t} Payroll`, null);
  await dialog.getByRole("button", { name: "Add line" }).click();
  await expect(dialog).toHaveCount(0);
  const totals = page.getByTestId("allocation-totals");
  await expect(totals).toContainText("Fixed shares: 60% of 100%");
  await expect(totals).toContainText("Not allocated: nothing");
  const lines = page.getByRole("region", { name: "Contract lines table" });
  await expect(lines.getByRole("row", { name: new RegExp(`${t} Ledger`) })).toContainText("7,203.60 USD");
  await expect(lines.getByRole("row", { name: new RegExp(`${t} Payroll`) })).toContainText("Shares the rest");
  await expect(lines.getByRole("row", { name: new RegExp(`${t} Payroll`) })).toContainText("4,802.40 USD");
  dialog = await addLine(`${t} Timesheets`, "50");
  await expect(dialog.getByTestId("allocation-after")).toContainText("over 100%");
  await expect(dialog.getByTestId("allocation-after")).toContainText("Fixed shares would total 110%");
  await expect(dialog.getByRole("dialog").or(dialog)).toBeVisible();
  await expectNoViolations(page, "Add a line dialog");
  await dialog.getByRole("button", { name: "Add line" }).click();
  await expect(dialog.getByLabel("Fixed share (%)")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.locator("#allocation_percent-error")).toContainText("110");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Add line" })).toBeFocused(); // focus back on the opener
  await expectNoViolations(page, "Contract lines");

  // The renewal it created takes its dates from the contract and refuses to change them itself.
  await page.getByRole("tab", { name: /^Renewal/ }).click();
  await expect(page.getByRole("region", { name: "Renewal cycles table" })).toContainText(day(35));
  await expectNoViolations(page, "Contract renewal tab");
  await page.getByRole("link", { name: `${t} Ledger suite` }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: `${t} Ledger suite` })).toBeVisible();
  const renewalId = page.url().split("/renewals/")[1].split("?")[0];
  await expect(page.getByTestId("renewal-contract-note")).toContainText(`Dates come from the contract ${t} Ledger suite.`);
  await page.getByRole("button", { name: "Edit terms" }).click();
  const terms = page.getByRole("dialog", { name: /Edit cycle 1 terms/ });
  await expect(terms.getByTestId("terms-contract-note")).toContainText(`come from the contract ${t} Ledger suite`);
  await terms.getByLabel("Renewal date").fill(day(90));
  await terms.getByRole("button", { name: "Save terms" }).click();
  await expect(terms.locator("#renewal_date-error")).toContainText(`This renewal takes its dates and auto-renewal from the contract ${t} Ledger suite. Change them on the contract.`);
  await expectNoViolations(page, "Renewal terms dialog refusing contract-owned fields");
  await terms.getByRole("button", { name: "Cancel" }).click();

  // Renew with the defaults: the next term starts the day after this one ends and drives the renewal.
  await page.goto(`/contracts/${id}/renew`);
  await expect(page.getByRole("heading", { level: 1, name: `Renew or cancel ${t} Ledger suite` })).toBeVisible();
  await expect(page.getByLabel("Name (required)")).toHaveValue(`${t} Ledger suite`);
  await expect(page.getByLabel("Start date")).toHaveValue(day(66));
  await expect(page.getByRole("checkbox", { name: new RegExp(`${t} Ledger`) })).toBeChecked();
  await expectNoViolations(page, "Renew page");
  await page.getByRole("button", { name: "Renew and create the next term" }).click();
  await expect(page).toHaveURL(/\/contracts\/[0-9a-f-]{36}$/);
  await expect(page).not.toHaveURL(new RegExp(`/contracts/${id}$`));
  await expect(page.getByRole("heading", { level: 1, name: `${t} Ledger suite`, exact: true })).toBeVisible();
  const successor = page.url().split("/contracts/")[1].split("?")[0];
  await said(page, `Renew: the next term ${t} Ledger suite was created and the renewal ${t} Ledger suite now takes its dates from it.`);
  await expect(page.getByTestId("contract-chain")).toContainText(`Renews ${t} Ledger suite (the previous term)`);
  await expect(page.locator("[data-status=signed]")).toBeVisible();
  await expect(page.getByTestId("drives-renewal")).toBeVisible();
  await page.getByRole("tab", { name: /^Renewal/ }).click();
  const cycles = page.getByRole("region", { name: "Renewal cycles table" });
  await expect(cycles.getByRole("row", { name: /^1 / })).toContainText("Renew");
  await expect(cycles.getByRole("row", { name: /^1 / })).toContainText(`ends ${day(65)} to `);

  // History lists the contract's own events and its lines' (children), each line with its kind.
  await page.goto(`/contracts/${id}?tab=history`);
  await expect(page.getByTestId("child-kind").first()).toBeVisible();
  await expect(page.getByRole("region", { name: /table/ }).first()).toContainText(`${t} Ledger`);
  await page.getByRole("checkbox", { name: "Only this record" }).check();
  await expect(page.getByTestId("child-kind")).toHaveCount(0);
  await expectNoViolations(page, "Contract History");
  journey = { t, id, successor, renewal: renewalId };
});

test("Cancel decision: notes are required, then the contract is Canceled", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  const c = await contract(page, `${t} Fax gateway`);
  await page.goto(`/contracts/${c.id}/renew`);
  await page.getByRole("radio", { name: /^Cancel:/ }).check();
  await expect(page.getByLabel("Name (required)")).toHaveCount(0); // no next term
  await page.getByRole("button", { name: `Cancel ${t} Fax gateway` }).click();
  await expect(page.getByLabel("Notes (required to cancel)")).toBeFocused();
  await expect(page.locator("#decision_notes-error")).toContainText("notes are required to cancel");
  await expectNoViolations(page, "Renew page, canceling with an error");
  await page.getByLabel("Notes (required to cancel)").fill("Nobody sends faxes any more.");
  await page.getByRole("button", { name: `Cancel ${t} Fax gateway` }).click();
  await expect(page).toHaveURL(new RegExp(`/contracts/${c.id}$`));
  await said(page, "Canceled. The decision is recorded.");
  await expect(page.locator("[data-status=cancelled]")).toContainText("Canceled");
  await expect(page.getByRole("link", { name: "Renew or cancel…" })).toHaveCount(0);
});

test("A stale edit is refused (412) with Load the latest version", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  const c = await contract(page, `${t} Wiki`);
  await page.goto(`/contracts/${c.id}/edit`);
  await expect(page.getByLabel("Name (required)")).toHaveValue(`${t} Wiki`);
  // Someone else saves first.
  await post(page, `contracts/${c.id}`, { cost_center: "CC-OTHER" }, "PATCH");
  await page.getByLabel("Cost center").fill("CC-MINE");
  await page.getByRole("button", { name: "Save contract" }).click();
  const stale = page.getByRole("alert").filter({ hasText: "Someone else saved changes to this contract" });
  await expect(stale).toBeVisible();
  await expectNoViolations(page, "Contract form, stale");
  await stale.getByRole("button", { name: "Load the latest version" }).click();
  await expect(page.getByLabel("Cost center")).toHaveValue("CC-OTHER");
  await page.getByLabel("Cost center").fill("CC-MINE");
  await page.getByRole("button", { name: "Save contract" }).click();
  await expect(page.getByRole("main").getByText("Contract saved.")).toBeVisible();
  await expect(page.getByTestId("shell-announcer")).toContainText("Contract saved.");
  await expect(page.getByRole("region", { name: "Cost" })).toContainText("CC-MINE");
});

test("Linking refusals are explained: another contract's renewal, and clearing a linked contract's end date", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  const first = await contract(page, `${t} Mail A`, { create_renewal_obligation: true });
  const second = await contract(page, `${t} Mail B`);
  await page.goto(`/contracts/${second.id}/edit`);
  await page.getByRole("radio", { name: "Link an existing renewal" }).check();
  await page.getByLabel("Renewal to link").selectOption({ label: `${t} Mail A (dates from ${t} Mail A)` });
  await page.getByRole("button", { name: "Save contract" }).click();
  // The refusal is on the renewal select itself (aria-invalid, described by the error).
  await expect(page.locator("#renewal_obligation-error")).toContainText(`This renewal already takes its dates from the contract ${t} Mail A.`);
  await expect(page.getByLabel("Renewal to link")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByLabel("Renewal to link")).toHaveAttribute("aria-describedby", /renewal_obligation-error/);
  await page.goto(`/contracts/${first.id}/edit`);
  await page.getByLabel("End date").fill("");
  await page.getByRole("button", { name: "Save contract" }).click();
  await expect(page.locator("#end_date-error")).toBeVisible();
});

test("A newly required custom field is filled in place on the renew page", async ({ browser, baseURL }) => {
  const admin = await as(browser, baseURL, ADMIN);
  const page = await pageAs(browser, baseURL, EDITOR);
  const t = token();
  const c = await contract(page, `${t} Survey tool`);
  const name = `budget_owner_${t.toLowerCase()}`;
  const field = await post(admin, "custom-fields", { label: `Budget owner ${t}`, name, type: "text", applies_to: ["contract"], required: true });
  try {
    await page.goto(`/contracts/${c.id}/renew`);
    const input = page.getByLabel(`Budget owner ${t} (required)`);
    await expect(input).toBeVisible();
    await page.getByRole("button", { name: "Renew and create the next term" }).click();
    await expect(input).toHaveAttribute("aria-invalid", "true");
    await expect(input).toBeFocused();
    await expectNoViolations(page, "Renew page with a custom field error");
    await input.fill("Finance");
    await page.getByRole("button", { name: "Renew and create the next term" }).click();
    await expect(page.getByTestId("contract-chain")).toContainText(`Renews ${t} Survey tool`);
    await expect(page.getByRole("region", { name: "Custom fields" })).toContainText("Finance");
  } finally {
    await post(admin, `custom-fields/${field.id}`, undefined, "DELETE");
  }
});

test("Contracts list: filters in the URL, columns and export; the Viewer reads but doesn't change", async ({ browser, baseURL }) => {
  const editor = await as(browser, baseURL, EDITOR);
  const t = journey?.t ?? token();
  if (!journey) await contract(editor, `${t} Ledger suite`);
  await editor.goto(`/contracts?q=${t}`);
  const table = editor.getByRole("region", { name: "Contracts table" });
  await expect(table.getByRole("row", { name: new RegExp(`${t} Ledger suite`) }).first()).toBeVisible();
  await editor.getByRole("button", { name: "Not linked to a renewal" }).click();
  await expect(editor).toHaveURL(/renewal_obligation=none/);
  await editor.getByRole("button", { name: "Not linked to a renewal" }).click();
  await expect(editor).not.toHaveURL(/renewal_obligation=none/);
  await expectNoViolations(editor, "Contracts list");
  const download = editor.waitForEvent("download");
  await editor.getByRole("button", { name: "Export CSV" }).click();
  expect((await download).suggestedFilename()).toMatch(/^softwarden-contracts-.*\.csv$/);

  const viewer = await pageAs(browser, baseURL, VIEWER);
  await viewer.goto("/contracts");
  await expect(viewer.getByRole("heading", { level: 1, name: "Contracts" })).toBeVisible();
  await expect(viewer.getByRole("link", { name: "Add contract" })).toHaveCount(0);
  if (journey) {
    await viewer.goto(`/contracts/${journey.successor}`);
    await expect(viewer.getByRole("heading", { level: 1, name: `${t} Ledger suite`, exact: true })).toBeVisible();
    await expect(viewer.getByRole("link", { name: "Edit" })).toHaveCount(0);
    await expect(viewer.getByRole("link", { name: "Renew or cancel…" })).toHaveCount(0);
    await viewer.getByRole("tab", { name: /^Lines/ }).click();
    await expect(viewer.getByRole("button", { name: "Add line" })).toHaveCount(0);
    await expect(viewer.getByRole("button", { name: /^Remove/ })).toHaveCount(0);
    await expectNoViolations(viewer, "Contract page as a Viewer");
  }
  await viewer.goto("/contracts/new");
  await expect(viewer.getByRole("heading", { level: 1, name: "You can't change contracts" })).toBeVisible();
});

test("Contracts are in the palette, the search results and a record's Map", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  const a = await app(page, `${t} Atlas`);
  const c = await contract(page, `${t} Atlas license`);
  await post(page, `contracts/${c.id}/lines`, { record: a.id, allocation_percent: "100" });

  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Needs attention" })).toBeVisible();
  await page.getByRole("button", { name: "Search or jump to…" }).click();
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await palette.getByRole("combobox", { name: "Search records, pages and actions" }).fill(`${t} Atlas license`);
  const option = palette.getByRole("group", { name: "Contracts" }).getByRole("option", { name: new RegExp(`${t} Atlas license`) });
  await expect(option).toBeVisible();
  await option.click();
  await expect(page).toHaveURL(new RegExp(`/contracts/${c.id}$`));

  await page.goto(`/search?q=${t}%20Atlas%20license`);
  const results = page.getByRole("region", { name: /Contracts/ });
  await expect(results.getByRole("link", { name: `${t} Atlas license` })).toHaveAttribute("href", `/contracts/${c.id}`);

  await page.goto(`/applications/${a.id}?tab=map`);
  const connections = page.getByRole("region", { name: "Connections table" });
  await expect(connections.locator("[data-category=contract]")).toContainText("Contracts (1)");
  await expect(connections.getByRole("link", { name: `${t} Atlas license` })).toHaveAttribute("href", `/contracts/${c.id}`);
  await expect(connections).toContainText("100% fixed share");
});
