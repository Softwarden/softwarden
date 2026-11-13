import { expect, type Browser, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Contract and assessment details: a contract that can't be renewed turns Renew and
 *  Renegotiate off and points to why; the licence pool form and list filter search contracts (`contracts?q=`) instead
 *  of listing 200; a line refusal that names no field shown is said in the dialog; the relationship-type in-use dialog
 *  asks for 20 links and says "20 of N"; the assessment page words its paths from the snapshot's `type_name` without
 *  asking for the organization's relationship types. */

test.describe.configure({ timeout: 180_000 });

const EDITOR = "ct-editor@example.test";
let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "contracts" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}
const day = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
const contract = (page: Parameters<typeof post>[0], name: string, extra: Record<string, unknown> = {}) => post(page, "contracts", {
  name, amount: "1200.00", billing_frequency: "annual", start_date: day(-300), end_date: day(65), auto_renew: "yes", notice_value: 30, notice_unit: "days", ...extra,
});

test("A contract that can't be renewed: Renew and Renegotiate are off and point to the reason", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  const c = await contract(page, `${t} Archived term`);
  await post(page, `contracts/${c.id}/archive`, {});
  await page.goto(`/contracts/${c.id}/renew`);
  const note = page.getByTestId("renew-refusal");
  await expect(note).toContainText("This contract is archived.");
  for (const name of [/^Renew: /, /^Renegotiate: /]) {
    const radio = page.getByRole("radio", { name });
    await expect(radio).toBeDisabled();
    await expect(radio).toHaveAttribute("aria-describedby", "renew-refusal");
  }
  await expect(page.getByRole("radio", { name: /^Cancel: / })).toBeChecked();
  await expect(page.getByRole("group", { name: "Next term" })).toHaveCount(0);
  await expectNoViolations(page, "Renew page of a contract that can't be renewed");
  await page.context().close();
});

test("License pools search contracts by name in the form and in the list filter", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  const c = await contract(page, `${t} Pickme agreement`);
  const searched: string[] = [];
  page.on("request", (r) => { if (/\/api\/v1\/contracts\?/.test(r.url())) searched.push(r.url()); });

  await page.goto("/license-pools/new");
  const box = page.getByRole("combobox", { name: "Contract" });
  // Typing searches the API (after a short pause), rather than filtering a capped list.
  await Promise.all([
    page.waitForRequest((r) => /\/api\/v1\/contracts\?/.test(r.url()) && new URL(r.url()).searchParams.get("q") === `${t} Pick`),
    box.fill(`${t} Pick`),
  ]);
  await page.getByRole("option", { name: `${t} Pickme agreement` }).click();
  await expect(box).toHaveValue(`${t} Pickme agreement`);
  expect(searched.every((u) => !u.includes("page_size=200"))).toBeTruthy();
  await expectNoViolations(page, "License pool form with the contract search");

  // The list's filter: a link with ?contract= shows the contract by name, and can be cleared to Any.
  await page.goto(`/license-pools?contract=${c.id}`);
  const chip = page.getByRole("combobox", { name: "Contract" });
  await expect(chip).toHaveValue(`${t} Pickme agreement`);
  await chip.click();
  await page.getByRole("option", { name: "Any" }).click();
  await expect(page).not.toHaveURL(/contract=/);
  await page.context().close();
});

test("A line refusal that names no field shown is said in the dialog; the renewal tab counts one cycle", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const t = token();
  await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active" });
  const c = await contract(page, `${t} Refused lines`, { create_renewal_obligation: true });
  await page.goto(`/contracts/${c.id}`);
  await expect(page.getByRole("tab", { name: "Renewal (1 cycle)" })).toBeVisible();
  await page.getByRole("tab", { name: /^Lines/ }).click();
  await page.route(`**/api/v1/contracts/${c.id}/lines`, (route) => route.request().method() === "POST"
    ? route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "This contract changed while you were editing it. Reload it and try again." }) })
    : route.continue());
  await page.getByRole("button", { name: "Add line" }).click();
  const dialog = page.getByRole("dialog", { name: "Add a line" });
  await dialog.getByRole("combobox", { name: "Record (required)" }).fill(`${t} Ledger`);
  await dialog.getByRole("option", { name: new RegExp(`${t} Ledger`) }).click();
  await dialog.getByRole("radio", { name: /Shares the rest/ }).check(); // the percentage field is hidden
  await dialog.getByRole("button", { name: "Add line" }).click();
  await expect(dialog.getByRole("alert").filter({ hasText: "This contract changed while you were editing it." })).toContainText("Nothing was saved.");
  await expect(dialog.getByLabel("Fixed share (%)")).toHaveCount(0);
  await expectNoViolations(page, "Add line dialog with a refusal");
  await page.context().close();
});

test("The relationship type in-use dialog asks for 20 links and says how many there are", async ({ browser, baseURL }) => {
  const t = token().slice(-7);
  const name = `Mirrors ${t}`;
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const added = await post(admin, "relationship-types", { name });
  const a = await post(admin, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(admin, "applications", { name: `${t} B`, lifecycle: "active" });
  const link = await post(admin, "relationships", { type: added.slug, source: a.id, target: b.id });
  // As if 45 links used it: the API sends the first 20 and the count.
  let asked = "";
  await admin.route("**/api/v1/relationships?*", async (route) => {
    asked = route.request().url();
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, count: 45, has_more: true } });
  });
  await admin.goto(`/relationship-types?q=${encodeURIComponent(name)}`);
  await admin.getByRole("region", { name: "Relationship types table" }).getByRole("button", { name: `Delete ${name}` }).click();
  const del = admin.getByRole("dialog", { name: `Delete ${name}?` });
  await expect(del.getByRole("list", { name: `Links that use ${name}` }).getByRole("link")).toHaveCount(1);
  await expect(del.getByTestId("used-by-count")).toHaveText("Showing 1 of 45 links.");
  expect(asked).toContain(`type=${added.slug}`);
  expect(asked).toContain("limit=20");
  await del.getByRole("button", { name: "Cancel" }).click();
  await admin.unroute("**/api/v1/relationships?*");
  await post(admin, `relationships/${link.id}`, undefined, "DELETE");
  await post(admin, `relationship-types/${added.id}`, undefined, "DELETE");
  await admin.context().close();
});

test("The assessment page words its paths from the snapshot's type names, without fetching the relationship types", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const core = await post(page, "applications", { name: `${t} Core`, lifecycle: "active" });
  const portal = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  await post(page, "relationships", { type: "depends_on", source: portal.id, target: core.id });
  const assessment = await post(page, "assessments", { target: core.id, change_type: "change", title: `${t} Core upgrade` });
  const types: string[] = [];
  page.on("request", (r) => { if (/\/api\/v1\/relationship-types/.test(r.url())) types.push(r.url()); });
  // The snapshot's own words for the type (a renamed or deleted type keeps them).
  await page.route(`**/api/v1/assessments/${assessment.id}`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    for (const l of body.snapshot?.links ?? []) if (l.kind === "relationship") l.type_name = "Leans on";
    await route.fulfill({ response, json: body });
  });
  await page.goto(`/assessments/${assessment.id}`);
  await expect(page.getByRole("region", { name: "Affected set table" })).toContainText(`${t} Portal → leans on → ${t} Core`);
  expect(types).toEqual([]);
  await page.context().close();
});
