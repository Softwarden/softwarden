import { expect, type Browser, type Page, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, personId, post, token } from "./helpers";

/** Offboarding and access review. A person's footprint on their page (every section with its count, the
 *  licences named gap, no Admin-only sections for an Editor); the reassign flow (a dry run first with its planned rows,
 *  then apply, the footprint emptied); the access review's flags and filters in the URL; a Viewer's read-only person page
 *  and report; axe in both themes.
 *
 *  Reassigning moves every role a person holds in the organization and the report lists every record, so the spec runs
 *  in its own organization (`e2e/spec-org`, `offboarding`), and each test makes its own people and records. */

const EDITOR = "ob-editor@example.test";
const VIEWER = "ob-viewer@example.test";

test.describe.configure({ mode: "serial", timeout: 180_000 });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "offboarding" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const t = token();
const LEAVER = `${t} Leaver`;
const section = (page: Page, key: string) => page.locator(`[data-section=${key}]`);

test("An Editor reads a leaver's footprint, previews a reassign, then applies it", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const leaver = (await post(page, "people", { name: LEAVER })).id;
  await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active", business_owner: leaver, technical_owner: leaver });
  await post(page, "applications", { name: `${t} Intranet`, lifecycle: "active", technical_owner: leaver });

  // From People & Teams, the person's dialog links to their page.
  await page.goto("/admin/people");
  await page.getByLabel("Search").fill(t);
  await page.getByRole("button", { name: LEAVER, exact: true }).click();
  await page.getByRole("dialog").getByRole("link", { name: `Where ${LEAVER} appears and reassign their roles` }).click();
  await expect(page.getByRole("heading", { level: 1, name: LEAVER })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/people/${leaver}$`));

  const records = section(page, "records");
  await expect(records.getByRole("heading")).toHaveText(/\(2\)$/);
  await expect(records.getByRole("link", { name: `${t} Payroll` })).toBeVisible();
  await expect(records.getByRole("link", { name: `${t} Intranet` })).toBeVisible();
  await expect(section(page, "licences").locator("[data-gap]")).toHaveText("⚠Licenses: not read; needs User.Read.All");
  await expect(section(page, "account")).toHaveCount(0);
  await expect(section(page, "notification_rules")).toHaveCount(0);
  await expectNoViolations(page, "Person page, footprint (Editor)");

  // Reassign: the dry run comes first; a missing choice is said on its field.
  const opener = page.getByRole("button", { name: "Reassign roles…" });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: `Reassign ${LEAVER}'s roles` });
  await dialog.getByRole("button", { name: "Preview changes" }).click();
  await expect(dialog.getByRole("alert").filter({ hasText: "Choose the person who takes over." })).toBeVisible();
  await dialog.getByLabel("Hand over to (required)").fill("Mateo");
  await page.getByRole("option", { name: /Mateo Silva/ }).click();
  for (const role of ["IT steward", "Renewal follow-up"]) await dialog.getByRole("checkbox", { name: new RegExp(`^${role}`) }).uncheck();
  await expectNoViolations(page, "Reassign dialog, choices");
  await dialog.getByRole("button", { name: "Preview changes" }).click();
  await expect(dialog.getByTestId("reassign-summary")).toHaveText("2 items would change to Mateo Silva, 0 skipped, 0 refused.");
  await expect(dialog.locator("[data-row-status=planned]")).toHaveCount(2);
  await expect(dialog.getByText("Nothing has been saved yet.")).toBeVisible();
  await expectNoViolations(page, "Reassign dialog, preview");
  // Nothing was saved by the preview.
  const before = await (await page.request.get(`/api/v1/people/${leaver}/footprint`)).json();
  expect(before.sections.find((s: { key: string }) => s.key === "records").count).toBe(2);

  await dialog.getByRole("button", { name: "Apply 2 changes" }).click();
  await expect(dialog.getByTestId("reassign-done")).toContainText(`Reassigned 2 items from ${LEAVER} to Mateo Silva`);
  await expect(dialog.locator("[data-row-status=changed]")).toHaveCount(2);
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: `Reassigned 2 items from ${LEAVER} to Mateo Silva.` })).toHaveCount(1);
  await expect(records.getByRole("heading")).toHaveText(/\(0\)$/);
  await expect(records).toContainText("None.");
  await page.context().close();
});

test("Apply refuses a preview the records moved away from, and a refused row blocks Apply with the reason", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const leaver = (await post(page, "people", { name: `${t} Mover` })).id;
  await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active", business_owner: leaver });
  await page.goto(`/people/${leaver}`);
  await page.getByRole("button", { name: "Reassign roles…" }).click();
  const dialog = page.getByRole("dialog", { name: `Reassign ${t} Mover's roles` });
  await dialog.getByLabel("Hand over to (required)").fill("Ines");
  await page.getByRole("option", { name: /Ines Okafor/ }).click();
  for (const role of ["IT steward", "Renewal follow-up"]) await dialog.getByRole("checkbox", { name: new RegExp(`^${role}`) }).uncheck();
  await dialog.getByRole("button", { name: "Preview changes" }).click();
  await expect(dialog.getByTestId("reassign-summary")).toHaveText("1 item would change to Ines Okafor, 0 skipped, 0 refused.");

  // Someone gives the leaver another record after the preview: the real run's plan differs, so it is refused.
  await post(page, "applications", { name: `${t} Late`, lifecycle: "active", business_owner: leaver });
  await dialog.getByRole("button", { name: "Apply 1 change" }).click();
  await expect(dialog.getByTestId("reassign-stale")).toHaveText("The records changed since the preview. Preview again.");
  await expectNoViolations(page, "Reassign dialog, stale preview");
  const still = await (await page.request.get(`/api/v1/people/${leaver}/footprint`)).json();
  expect(still.sections.find((s: { key: string }) => s.key === "records").count).toBe(2);
  await dialog.getByRole("button", { name: "Preview again" }).click();
  await expect(dialog.getByTestId("reassign-summary")).toHaveText("2 items would change to Ines Okafor, 0 skipped, 0 refused.");
  await dialog.getByRole("button", { name: "Back" }).click();

  // A row the real run would refuse (the services refuse rarely, so the dry run's answer is given here): Apply stays
  // disabled, with the reason beside it.
  await page.route(`**/api/v1/people/${leaver}/reassign`, async (route) => {
    const body = route.request().postDataJSON();
    if (!body.dry_run) return route.continue();
    await route.fulfill({ json: {
      person: { id: leaver, name: `${t} Mover` }, to: { id: body.to, name: "Ines Okafor" }, roles: body.roles, dry_run: true, changed: 1, skipped: 0, refused: 1, plan_hash: "x".repeat(64),
      rows: [
        { type: "application", id: "1", name: `${t} Ledger`, path: null, roles: ["business_owner"], status: "planned", reason: "" },
        { type: "application", id: "2", name: `${t} Late`, path: null, roles: ["business_owner"], status: "refused", reason: "", errors: ["Simulated refusal."], field_errors: { business_owner: ["Choose an active person."] } },
      ],
    } });
  });
  await dialog.getByRole("button", { name: "Preview changes" }).click();
  await expect(dialog.locator("[data-row-status=refused]")).toContainText("Simulated refusal.");
  await expect(dialog.locator("[data-row-status=refused]")).toContainText("Business owner: Choose an active person.");
  await expect(dialog.getByRole("button", { name: "Apply 1 change" })).toBeDisabled();
  await expect(dialog.locator("#apply-why")).toHaveText("Apply needs every row to pass. Fix the refused rows, or untick their roles, and preview again.");
  await expectNoViolations(page, "Reassign dialog, refused row");
  await page.unroute(`**/api/v1/people/${leaver}/reassign`);
  await page.context().close();
});

test("The access review flags records and keeps its filters in the URL", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, EDITOR);
  const gone = (await post(page, "people", { name: `${t} Gone` })).id;
  await post(page, "applications", { name: `${t} Wiki`, lifecycle: "active", business_owner: gone, technical_owner: gone });
  await post(page, "applications", { name: `${t} Orphan`, lifecycle: "active" });
  await post(page, `people/${gone}`, { active: false }, "PATCH");
  const row = (name: string) => page.getByRole("region", { name: "Access review table" }).getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${name}`) }) });

  await page.goto("/");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Access review" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Access review" })).toBeVisible();
  await expect(page.getByTestId("access-counts")).toContainText("records in scope");
  await expect(row(`${t} Wiki`).locator("[data-flag]")).toHaveText(["⚠Inactive owner"]);
  await expect(row(`${t} Wiki`)).toContainText("Marked inactive");
  await expect(row(`${t} Orphan`).locator("[data-flag]")).toHaveText(["⚠No owner"]);
  await expectNoViolations(page, "Access review");

  await page.getByLabel("Flag").selectOption("no_owner");
  await expect(page).toHaveURL(/flag=no_owner/);
  await expect(row(`${t} Orphan`)).toHaveCount(1);
  await expect(row(`${t} Wiki`)).toHaveCount(0);
  await page.getByLabel("Flag").selectOption("inactive_owner");
  await page.getByLabel("Owner", { exact: true }).fill(`${t} Gone`);
  await page.getByRole("option", { name: new RegExp(`${t} Gone \\(inactive\\)`) }).click();
  await expect(page).toHaveURL(new RegExp(`owner=${gone}`));
  await page.getByLabel("Include retired records").check();
  await expect(page).toHaveURL(/include_retired=true/);
  await page.reload();
  await expect(page.getByLabel("Flag")).toHaveValue("inactive_owner");
  await expect(page.getByLabel("Include retired records")).toBeChecked();
  await expect(row(`${t} Wiki`)).toHaveCount(1);
  await expect(row(`${t} Orphan`)).toHaveCount(0);
  await page.getByLabel("Kind").selectOption("integration");
  await expect(page.getByText("No records match these filters")).toBeVisible();
  await expectNoViolations(page, "Access review, no match");
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/access-review$/);
  // An owner's name opens their person page.
  await row(`${t} Wiki`).getByRole("link", { name: `${t} Gone` }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: `${t} Gone` })).toBeVisible();
  await page.context().close();
});

test("A Viewer reads a footprint and the access review and can't reassign", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, VIEWER);
  const mateo = await personId(page, "Mateo Silva");
  await page.goto(`/people/${mateo}`);
  await expect(page.getByRole("heading", { level: 1, name: "Mateo Silva" })).toBeVisible();
  await expect(page.getByTestId("read-only")).toHaveText("You can read where Mateo Silva appears. Editors and Admins reassign their roles.");
  await expect(page.getByRole("button", { name: "Reassign roles…" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Export CSV" })).toBeVisible();
  await expect(section(page, "account")).toHaveCount(0);
  await expectNoViolations(page, "Person page (Viewer)");
  await page.goto("/access-review");
  await expect(page.getByRole("region", { name: "Access review table" })).toBeVisible();
  await expectNoViolations(page, "Access review (Viewer)");
  await page.context().close();
});
