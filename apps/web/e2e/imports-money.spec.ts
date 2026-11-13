import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Importing contracts and licence pools. The kinds on the upload step (with the `contracts`
 *  capability), each kind's template, a contract file with lines and a new renewal whose preview shows the annualised
 *  cost, the cancellation deadline with its label and the split over the lines (a row over 100% refused under `items`),
 *  commit and the link to the contract; a pool file whose preview says the overuse in words, and its link; the column
 *  mapping needing the SKU name; and a file over 500 rows, whose notice (no rehearsal) is shown (a patched response).
 *  axe in both themes. */

test.describe.configure({ mode: "serial", timeout: 180_000 });

const csv = (headers: string[], rows: string[][]) => [headers, ...rows].map((r) => r.join(",")).join("\n") + "\n";
const day = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
const stepHeading = (page: Page, name: string | RegExp) => page.getByRole("heading", { level: 2, name });
const commitButton = (page: Page) => page.getByRole("button", { name: /^(Commit \d+ changes?|Retry commit)$/ });

async function startUpload(page: Page, kind: "Contracts" | "License pools") {
  await page.goto("/imports/new");
  await expect(stepHeading(page, "Upload")).toBeVisible();
  await page.getByRole("radio", { name: kind }).check();
}
async function upload(page: Page, name: string, content: string) {
  await page.getByLabel("CSV file").setInputFiles({ name, mimeType: "text/csv", buffer: Buffer.from(content) });
  await page.getByRole("button", { name: "Upload" }).click();
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]{36}$/);
}

const t = token();

test("Contracts: the template, a file with lines and a new renewal, the computed preview, a refused row, commit and the link", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await post(page, "applications", { name: `${t} Mail`, lifecycle: "active" });
  await post(page, "applications", { name: `${t} Chat`, lifecycle: "active" });

  await page.goto("/imports/new");
  await expect(page.getByRole("radio", { name: "Contracts" })).toBeVisible();
  await expect(page.getByRole("radio", { name: "License pools" })).toBeVisible();
  await page.getByRole("radio", { name: "Contracts" }).check();
  await expect(page.getByText(/Minimum per row: name\. Lines go in items/)).toBeVisible();
  const [request, download] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("imports/template.csv?kind=contract")),
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download template (.csv) for contracts" }).click(),
  ]);
  expect(request.url()).toMatch(/kind=contract$/);
  const template = readFileSync((await download.path())!, "utf-8").replace(/^\uFEFF/, "").split(/\r?\n/)[0];
  for (const column of ["name", "vendor", "amount", "billing_frequency", "items", "create_renewal_obligation"]) expect(template.split(",")).toContain(column);
  await expectNoViolations(page, "Import upload step, contracts chosen");

  const headers = ["name", "contract_number", "amount", "currency", "billing_frequency", "start_date", "end_date", "auto_renew", "notice_value", "notice_unit", "status", "items", "create_renewal_obligation"];
  await upload(page, "contracts.csv", csv(headers, [
    [`${t} Suite`, `${t}-1`, "1200", "USD", "monthly", day(-30), day(335), "yes", "30", "days", "active", `${t} Mail=70; ${t} Chat`, "true"],
    [`${t} Overbooked`, `${t}-2`, "100", "USD", "annual", day(-30), day(335), "yes", "30", "days", "active", `${t} Mail=80; ${t} Chat=40`, ""],
  ]));
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: "Import contracts" })).toBeVisible();
  const rows = page.getByRole("region", { name: "Import rows table" });
  const suite = rows.getByRole("row", { name: new RegExp(`${t} Suite`) });
  const computed = suite.getByTestId("computed");
  await expect(computed).toContainText("Annualized cost: 14,400.00 USD");
  await expect(computed).toContainText(`Cancellation deadline: ${day(305)}`);
  await expect(computed).toContainText("Calculated");
  await expect(computed).toContainText(`${t} Mail: 70% (10,080.00 USD a year)`);
  await expect(computed).toContainText(`${t} Chat: 30% (4,320.00 USD a year)`);
  // The fixed shares over 100% are the service's refusal, on the row, under its column.
  const over = rows.getByRole("row", { name: new RegExp(`${t} Overbooked`) });
  await expect(over).toContainText("Error");
  await expect(over.locator("li").filter({ hasText: /^✕ Error: Items: / })).toHaveCount(1);
  await expect(commitButton(page)).toBeDisabled();
  await expectNoViolations(page, "Contract import preview with computed values and a refused row");
  await over.getByRole("checkbox", { name: new RegExp(`Exclude row 3, ${t} Overbooked`) }).check();
  await expect(commitButton(page)).toBeEnabled();
  await commitButton(page).click();

  await expect(stepHeading(page, "Results")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open Contracts" })).toBeVisible();
  const created = page.getByRole("region", { name: "Import rows table" }).getByRole("link", { name: `${t} Suite` });
  await expect(created).toHaveAttribute("href", /^\/contracts\/[0-9a-f-]{36}$/);
  await expectNoViolations(page, "Contract import results");
  await created.click();
  await expect(page.getByRole("heading", { level: 1, name: `${t} Suite` })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Renewal (1 cycle)" })).toBeVisible(); // made by create_renewal_obligation

  await page.goto("/imports");
  await expect(page.getByRole("row", { name: /contracts\.csv/ }).first()).toContainText("Contracts");
  await page.context().close();
});

test("License pools: the template, overuse in words, commit and the link; the mapping needs the SKU name", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await startUpload(page, "License pools");
  const [request] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("imports/template.csv?kind=license_pool")),
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download template (.csv) for license pools" }).click(),
  ]);
  expect(request.url()).toMatch(/kind=license_pool$/);
  await upload(page, "pools.csv", csv(["application", "sku_name", "seats_purchased", "seats_assigned"], [[`${t} Mail`, `${t} E5`, "10", "12"]]));
  await expect(stepHeading(page, "Preview")).toBeVisible();
  const row = page.getByRole("region", { name: "Import rows table" }).getByRole("row", { name: new RegExp(`${t} E5`) });
  await expect(row.getByTestId("computed")).toContainText("Utilization: 12 of 10 seats (120%)");
  await expect(row.getByTestId("computed")).toContainText("Overused by 2");
  await expectNoViolations(page, "License pool import preview");
  await commitButton(page).click();
  await expect(stepHeading(page, "Results")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open License pools" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Import rows table" }).getByRole("link", { name: `${t} E5` })).toHaveAttribute("href", /^\/license-pools\/[0-9a-f-]{36}$/);

  // Other headings: the mapping says the SKU name is required and refuses to go on without it.
  await startUpload(page, "License pools");
  await upload(page, "pools-mapped.csv", csv(["App", "Plan", "Bought"], [[`${t} Chat`, `${t} Plan`, "5"]]));
  await expect(stepHeading(page, "Map columns")).toBeVisible();
  await expect(page.getByText(/SKU name is required\./i)).toBeVisible();
  for (const h of ["App", "Plan", "Bought"]) await page.getByLabel(`Field for column ${h}`).selectOption("ignore");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("alert")).toContainText(/Map a column to SKU name \(required\)/i);
  await page.getByLabel("Field for column App").selectOption("application");
  await page.getByLabel("Field for column Plan").selectOption("sku_name");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(stepHeading(page, "Preview")).toBeVisible();
  await page.context().close();
});

test("A file over 500 rows isn't rehearsed: the preview says the rules are checked at commit", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await startUpload(page, "Contracts");
  await upload(page, "large.csv", csv(["name", "amount"], [[`${t} Large one`, "100"]]));
  await expect(stepHeading(page, "Preview")).toBeVisible();
  const id = page.url().split("/imports/")[1];
  // Building 501 rows here would take the API a while; the run's own response is patched as a large one comes back.
  const notice = "This file has more than 500 rows, so the contract rules (allocations already on a contract, renewal links, seat sync per SKU) are checked when you commit, not in this preview, and the annualized cost and deadline of new rows aren't shown. If a row breaks one, the commit names it and nothing is imported.";
  await page.route(`**/api/v1/imports/${id}`, async (route) => {
    const response = await route.fetch();
    const run = await response.json();
    run.counts = { ...run.counts, rehearsed: 0 };
    run.panels = { ...run.panels, notices: [notice] };
    await route.fulfill({ response, json: run });
  });
  // A fresh visit (not the upload's hand-over, which carries the run in the history state) loads the run.
  await page.goto("/imports");
  await page.goto(`/imports/${id}`);
  const note = page.getByTestId("import-notices");
  await expect(note).toContainText("Checked at commit, not in this preview");
  await expect(note).toContainText("more than 500 rows");
  await expect(note).toHaveAttribute("role", "note");
  await expectNoViolations(page, "Large contract import notice");
  await page.context().close();
});
