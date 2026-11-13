import { expect, test, type Page } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

/** The renewal list, form and page, through the screens, with the deadline rules. */

test.describe.configure({ mode: "serial" });

const app = (page: Page, name: string) => post(page, "applications", { name, lifecycle: "active" });
/** A save notice: the announced status text (the visible copy beside it is aria-hidden). */
const said = (page: Page, text: string) => expect(page.getByRole("status").filter({ hasText: text })).toHaveCount(1);

test("Add a renewal: the form previews the deadline with the month-end rule, and the page explains it", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const hr = await app(page, `${t} HR`);
  await page.goto("/renewals/new");
  await page.getByLabel("Name (required)").fill(`${t} Workday subscription`);
  await page.getByRole("radio", { name: "Yes" }).check();
  await page.getByRole("combobox", { name: "Covered applications" }).fill(`${t} HR`);
  await page.getByRole("option", { name: new RegExp(`${t} HR`) }).click();
  await expect(page.getByRole("list", { name: "Covered applications" })).toContainText(`${t} HR`);
  await page.getByLabel("Renewal date").fill("2027-03-31");
  await page.getByLabel("Notice period, number").fill("1");
  await expect(page.getByRole("status").filter({ hasText: "Action deadline" })).toContainText("2027-02-28 (Calculated)");
  await page.getByLabel("Notice period, number").fill("2");
  await expect(page.getByRole("status").filter({ hasText: "Action deadline" })).toContainText("2027-01-31 (Calculated)");
  await page.getByRole("button", { name: "Add renewal" }).click();

  await expect(page.getByRole("heading", { level: 1, name: `${t} Workday subscription` })).toBeVisible();
  // Shown on the page and said once, by the shell's announcer.
  await expect(page.getByRole("main").getByText("Renewal added. Action deadline: 2027-01-31 (Calculated).")).toBeVisible();
  await expect(page.getByTestId("shell-announcer")).toContainText("Renewal added. Action deadline: 2027-01-31 (Calculated).");
  const deadline = page.getByRole("region", { name: "Action deadline" });
  await expect(deadline).toContainText("Renewal date 2027-03-31 minus notice period 2 months = 2027-01-31.");
  await expect(deadline).toContainText("overdue from 2027-02-01");
  await expect(page.getByRole("region", { name: /Covers/ })).toContainText(`${t} HR`);
  // The responsible person and follow-up weren't set: they show as gaps, and the renewal still saved.
  await expect(page.getByRole("region", { name: "Gaps" })).toContainText("No responsible person");
  expect(hr.id).toBeTruthy();
});

test("Decision is not completion; completion then the next cycle keeps history", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const hr = await app(page, `${t} HR`);
  const r = await post(page, "renewals", {
    name: `${t} Zoom Pro`, auto_renew: "yes", renewal_date: "2027-03-31", notice_value: 2, notice_unit: "months",
    term_value: 1, term_unit: "years", applications: [hr.id], responsible: await personId(page, "Dana Ortiz"),
  });
  await page.goto(`/renewals/${r.id}`);
  const completion = page.getByRole("group", { name: "2. Completion" });
  await expect(completion).toContainText("Record the decision first.");
  const decision = page.getByRole("group", { name: "1. Decision" });
  await decision.getByRole("radio", { name: "Cancel" }).check();
  await decision.getByLabel("Notes").fill("Moving to Teams");
  await decision.getByRole("button", { name: "Save decision" }).click();
  await said(page, "Decision saved: Cancel. The cycle isn't complete until you record what was done.");
  // Still an open cycle with a deadline; the completion step is now available.
  await expect(page.getByRole("region", { name: "Action deadline" })).toContainText("in ");
  await completion.getByRole("button", { name: "Record completion" }).click();
  await expect(completion.getByRole("alert")).toContainText("Enter the completion date.");
  const today = new Date().toISOString().slice(0, 10);
  await completion.getByLabel("Completed on (required)").fill(today);
  await completion.getByLabel("Evidence or reference (required)").fill("Cancellation email to the vendor");
  await completion.getByRole("button", { name: "Record completion" }).click();
  await said(page, "Completion recorded.");
  await expect(page.getByRole("region", { name: "Gaps" })).toContainText("This cycle is complete. Establish the next cycle");

  await page.getByRole("button", { name: "Establish next cycle" }).click();
  const dialog = page.getByRole("dialog", { name: "Establish cycle 2" });
  await expect(dialog.getByLabel("Renewal date")).toHaveValue("2028-03-31");
  await dialog.getByRole("button", { name: "Establish next cycle" }).click();
  await said(page, "Cycle 2 established.");
  await expect(page.getByRole("tab", { name: "Current cycle (#2)" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Action deadline" })).toContainText("2028-01-31");
  await page.getByRole("tab", { name: "Cycle history (1)" }).click();
  const history = page.getByRole("region", { name: "Cycle history table" });
  await expect(history).toContainText("2027-03-31");
  await expect(history).toContainText("Cancel");
  await expect(history).toContainText("Cancellation email to the vendor");
  await page.getByRole("tab", { name: "Audit activity" }).click();
  await expect(page.getByRole("region", { name: "Audit activity table" })).toContainText("Next cycle established");
});

test("Edit terms and covered applications on the open cycle", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const one = await app(page, `${t} One`);
  await app(page, `${t} Two`);
  const r = await post(page, "renewals", { name: `${t} Suite`, auto_renew: "yes", renewal_date: "2027-06-30", applications: [one.id] });
  await page.goto(`/renewals/${r.id}`);
  await expect(page.getByRole("region", { name: "Gaps" })).toContainText("Assumed deadline");
  await page.getByRole("button", { name: "Edit terms" }).click();
  const terms = page.getByRole("dialog", { name: "Edit cycle 1 terms" });
  await terms.getByLabel("Notice period, number").fill("90");
  await terms.getByLabel("Notice period, unit").selectOption("days");
  await terms.getByRole("button", { name: "Save terms" }).click();
  await said(page, "Cycle terms saved.");
  await expect(page.getByRole("region", { name: "Action deadline" })).toContainText("2027-04-01");
  await expect(page.getByRole("region", { name: "Gaps" })).not.toContainText("Assumed deadline");
  await expect(page.getByRole("button", { name: "Edit terms" })).toBeFocused();

  await page.getByRole("button", { name: "Edit covered applications" }).click();
  const covers = page.getByRole("dialog", { name: "Edit covered applications" });
  await covers.getByRole("combobox", { name: "Covered applications" }).fill(`${t} Two`);
  await covers.getByRole("option", { name: new RegExp(`${t} Two`) }).click();
  await covers.getByRole("button", { name: `Remove ${t} One` }).click();
  await covers.getByRole("button", { name: "Save applications" }).click();
  await expect(page.getByRole("region", { name: /Covers/ })).toContainText(`${t} Two`);
  await expect(page.getByRole("region", { name: /Covers/ })).not.toContainText(`${t} One`);
});

test("List: filters in the URL, gaps and deadline labels; Viewer reads only", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const soon = new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10);
  await post(page, "renewals", { name: `${t} Overdue`, auto_renew: "yes", explicit_deadline: soon });
  await post(page, "renewals", { name: `${t} Nothing`, auto_renew: "unknown" });
  await page.goto(`/renewals?q=${t}`);
  const table = page.getByRole("region", { name: "Renewals table" });
  await expect(table.getByRole("row")).toHaveCount(3);
  const overdue = table.getByRole("row", { name: new RegExp(`${t} Overdue`) });
  await expect(overdue).toContainText("Entered");
  await expect(overdue).toContainText("Overdue by 3 days");
  const nothing = table.getByRole("row", { name: new RegExp(`${t} Nothing`) });
  await expect(nothing).toContainText("No deadline");
  await expect(nothing).toContainText("Auto-renewal unknown");
  await page.getByRole("button", { name: "Overdue" }).click();
  await expect(page).toHaveURL(/quick=overdue/);
  await expect(table.getByRole("row")).toHaveCount(2);
  await page.reload();
  await expect(page.getByRole("button", { name: "Overdue" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Clear filters" }).click();
  await page.getByLabel("Search renewals").fill(`${t}-nothing-matches`);
  await expect(page.getByRole("main").getByText("No renewals match these filters")).toBeVisible();

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/renewals?q=${t}`);
  await expect(viewer.getByRole("region", { name: "Renewals table" })).toContainText(`${t} Overdue`);
  await expect(viewer.getByRole("link", { name: "Add renewal" })).toHaveCount(0);
  await viewer.getByRole("link", { name: `${t} Overdue` }).click();
  await expect(viewer.getByRole("button", { name: "Save decision" })).toHaveCount(0);
  await expect(viewer.getByRole("link", { name: "Edit" })).toHaveCount(0);
  await viewer.goto("/renewals/new");
  await expect(viewer.getByRole("heading", { name: "You can't change renewals" })).toBeVisible();
});

test("Renewals tab on application detail", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const hr = await app(page, `${t} HR`);
  await post(page, "renewals", { name: `${t} HR license`, auto_renew: "no", renewal_date: "2027-06-30", applications: [hr.id] });
  await page.goto(`/applications/${hr.id}`);
  await page.getByRole("tab", { name: "Renewals (1)" }).click();
  await expect(page).toHaveURL(/tab=renewals/);
  await expect(page.getByRole("tabpanel")).toContainText(`${t} HR license`);
  await expect(page.getByRole("tabpanel")).toContainText("Renewal action");
});
