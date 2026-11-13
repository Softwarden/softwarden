import { expect, test, type Page, type Request } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Every edit form sends the ETag it read as If-Match, and a save after someone else saved
 *  the same record shows "changed since you opened it" with "Load the latest version" instead of reverting their change.
 *  The e2e servers require If-Match from a signed-in session (playwright.config.ts), so a write without it fails with 428
 *  in every journey; these tests check the header is the record's ETag (not `*`) and the stale state on a vendor and on
 *  a renewal cycle. */

const STALE = (what: string) => `Someone else saved changes to this ${what} after you opened it`;
/** A real ETag: a quoted revision, never the "any revision" `*`. */
const ETAG = /^"[^"*]+"$/;

/** The next PATCH to `path` (an API path ending), resolved with its If-Match header. */
function patchTo(page: Page, path: string) {
  return page.waitForRequest((r: Request) => r.method() === "PATCH" && new URL(r.url()).pathname === `/api/v1/${path}`);
}

test("A vendor saved by someone else after the form opened: the save says so and loads theirs", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const other = await pageAs(browser, baseURL, "editor@example.test");
  const vendor = await post(page, "vendors", { name: `${t} Contoso`, website: "https://old.example" });
  await page.goto(`/vendors/${vendor.id}`);
  await page.getByRole("button", { name: "Edit vendor" }).click();
  const dialog = page.getByRole("dialog", { name: `Edit ${t} Contoso` });
  await expect(dialog.getByLabel("Website")).toHaveValue("https://old.example");

  // Someone else saves the vendor while this form is open.
  await post(other, `vendors/${vendor.id}`, { website: "https://theirs.example" }, "PATCH");

  await dialog.getByLabel("Website").fill("https://mine.example");
  const sent = patchTo(page, `vendors/${vendor.id}`);
  await dialog.getByRole("button", { name: "Save" }).click();
  expect((await sent).headers()["if-match"]).toMatch(ETAG);
  await expect(dialog.getByRole("alert")).toContainText(STALE("vendor"));
  // Their change is kept: nothing of this form was saved.
  expect((await (await page.request.get(`/api/v1/vendors/${vendor.id}`)).json()).website).toBe("https://theirs.example");
  await expectNoViolations(page, "The vendor form after a stale save");

  await dialog.getByRole("button", { name: "Load the latest version" }).click();
  await expect(dialog.getByLabel("Website")).toHaveValue("https://theirs.example");
  await expect(dialog.getByText(STALE("vendor"))).toHaveCount(0);
  await dialog.getByLabel("Website").fill("https://mine.example");
  const again = patchTo(page, `vendors/${vendor.id}`);
  await dialog.getByRole("button", { name: "Save" }).click();
  expect((await again).headers()["if-match"]).toMatch(ETAG);
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText(`${t} Contoso saved.`).first()).toBeVisible();
  expect((await (await page.request.get(`/api/v1/vendors/${vendor.id}`)).json()).website).toBe("https://mine.example");
});

test("A renewal cycle saved by someone else after the terms opened: the save says so and loads theirs", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const other = await pageAs(browser, baseURL, "admin@example.test");
  const app = await post(page, "applications", { name: `${t} HR`, lifecycle: "active" });
  const r = await post(page, "renewals", { name: `${t} Suite`, auto_renew: "yes", renewal_date: "2027-06-30", applications: [app.id] });
  await page.goto(`/renewals/${r.id}`);
  await page.getByRole("button", { name: "Edit terms" }).click();
  const terms = page.getByRole("dialog", { name: "Edit cycle 1 terms" });
  await expect(terms.getByLabel("Notice period, number")).toHaveValue("");

  await post(other, `renewals/${r.id}/cycles/${r.cycle.id}`, { notice_value: 30, notice_unit: "days" }, "PATCH");

  await terms.getByLabel("Notice period, number").fill("90");
  await terms.getByLabel("Notice period, unit").selectOption("days");
  const sent = patchTo(page, `renewals/${r.id}/cycles/${r.cycle.id}`);
  await terms.getByRole("button", { name: "Save terms" }).click();
  expect((await sent).headers()["if-match"]).toMatch(ETAG);
  await expect(terms.getByRole("alert")).toContainText(STALE("renewal"));
  const stored = await (await page.request.get(`/api/v1/renewals/${r.id}`)).json();
  expect(stored.cycle.notice).toEqual({ value: 30, unit: "days" });
  await expectNoViolations(page, "The cycle terms after a stale save");

  await terms.getByRole("button", { name: "Load the latest version" }).click();
  await expect(terms.getByLabel("Notice period, number")).toHaveValue("30");
  await terms.getByLabel("Notice period, number").fill("90");
  await terms.getByRole("button", { name: "Save terms" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Cycle terms saved." })).toHaveCount(1);
  expect((await (await page.request.get(`/api/v1/renewals/${r.id}`)).json()).cycle.notice).toEqual({ value: 90, unit: "days" });
});

test("Edit forms opened from a list save with the ETag they read", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const person = await post(page, "people", { name: `${t} Priya` });
  const team = await post(page, "teams", { name: `${t} Platform` });
  const category = await post(page, "categories", { name: `${t} Finance` });

  // A person (People & Teams).
  await page.goto("/admin/people");
  await page.getByRole("button", { name: `Edit ${t} Priya` }).click();
  let dialog = page.getByRole("dialog", { name: `Edit ${t} Priya` });
  await dialog.getByLabel("Title").fill("Engineer");
  let sent = patchTo(page, `people/${person.id}`);
  await dialog.getByRole("button", { name: "Save" }).click();
  expect((await sent).headers()["if-match"]).toMatch(ETAG);
  await expect(dialog).toHaveCount(0);

  // A team.
  await page.goto("/admin/people?tab=teams");
  await page.getByRole("button", { name: `Edit ${t} Platform` }).click();
  dialog = page.getByRole("dialog", { name: `Edit ${t} Platform` });
  await dialog.getByLabel("Description").fill("Runs the platform");
  sent = patchTo(page, `teams/${team.id}`);
  await dialog.getByRole("button", { name: "Save" }).click();
  expect((await sent).headers()["if-match"]).toMatch(ETAG);
  await expect(dialog).toHaveCount(0);

  // A software category.
  await page.goto("/categories");
  await page.getByRole("button", { name: `Edit ${t} Finance` }).click();
  dialog = page.getByRole("dialog", { name: `Edit ${t} Finance` });
  await dialog.getByLabel("Description").fill("Money software");
  sent = patchTo(page, `categories/${category.id}`);
  await dialog.getByRole("button", { name: "Save" }).click();
  expect((await sent).headers()["if-match"]).toMatch(ETAG);
  await expect(dialog).toHaveCount(0);

  // Organization settings: the page's ETag, read again after each save, so a second save isn't refused as stale. The
  // values are saved as they are (other specs share the organization and run alongside).
  await page.goto("/admin/organization");
  await expect(page.getByLabel("Organization name")).not.toHaveValue("");
  for (let i = 0; i < 2; i++) {
    sent = patchTo(page, "organization");
    await page.getByRole("button", { name: "Save settings" }).click();
    expect((await sent).headers()["if-match"]).toMatch(ETAG);
    await expect(page.getByRole("status").filter({ hasText: "Settings saved." })).toHaveCount(1);
    await expect(page.getByText(STALE("organization"))).toHaveCount(0);
  }
});

test("Organization settings never revert another Admin's change, even with the organization cached by another screen", async ({ browser, baseURL }) => {
  // An organization of its own: these settings belong to the whole organization (e2e/spec-org `if_match`, reset to a
  // 90-day review interval).
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "e2e/spec-org", { which: "if_match" });
  const page = await pageAs(browser, baseURL, "im-admin@example.test");
  const other = await pageAs(browser, baseURL, "im-admin2@example.test");
  const interval = page.getByLabel("Review interval (days)");

  // 1. A screen that caches the organization for minutes (the record form reads the compliance switch from it).
  const cached = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/organization" && r.request().method() === "GET");
  await page.goto("/applications/new");
  await cached;
  // 2. Another Admin changes a setting.
  await post(other, "organization", { review_interval_days: 200 }, "PATCH");
  // 3. Organization settings, opened in the same app (the cache is still there): the form shows their value, and a
  // save keeps it.
  await page.getByRole("navigation").getByRole("link", { name: "Organization settings" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Organization settings" })).toBeVisible();
  await expect(interval).toHaveValue("200");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Settings saved." })).toHaveCount(1);
  expect((await (await page.request.get("/api/v1/organization")).json()).review_interval_days).toBe(200);

  // 4. Another Admin changes it while the form is open: the save says so and changes nothing.
  await post(other, "organization", { review_interval_days: 120 }, "PATCH");
  await interval.fill("60");
  const sent = patchTo(page, "organization");
  await page.getByRole("button", { name: "Save settings" }).click();
  expect((await sent).headers()["if-match"]).toMatch(ETAG);
  await expect(page.getByRole("alert").filter({ hasText: STALE("organization") })).toBeVisible();
  expect((await (await page.request.get("/api/v1/organization")).json()).review_interval_days).toBe(120);
  await expectNoViolations(page, "Organization settings after a stale save");
  await page.getByRole("button", { name: "Load the latest version" }).click();
  await expect(interval).toHaveValue("120");
  await interval.fill("60");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Settings saved." })).toHaveCount(1);
  expect((await (await page.request.get("/api/v1/organization")).json()).review_interval_days).toBe(60);
});

test("A delete confirmation sends the ETag of the record as it opened", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const department = await post(page, "departments", { name: `${t} Legal` });
  await page.goto("/admin/people?tab=departments");
  await page.getByRole("button", { name: `Delete ${t} Legal` }).click();
  const dialog = page.getByRole("dialog", { name: `Delete ${t} Legal?` });
  const sent = page.waitForRequest((r) => r.method() === "DELETE" && new URL(r.url()).pathname === `/api/v1/departments/${department.id}`);
  await dialog.getByRole("button", { name: `Delete ${t} Legal` }).click();
  expect((await sent).headers()["if-match"]).toMatch(ETAG);
  await expect(dialog).toHaveCount(0);
  expect((await page.request.get(`/api/v1/departments/${department.id}`)).status()).toBe(404);
});
