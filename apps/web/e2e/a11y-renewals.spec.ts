import { expectNoViolations } from "./axe";
import { expect, test, type Page } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

/** Axe (WCAG 2.2 AA tags) on every renewal screen and dialog, as Admin so every control is present. */

// axe runs in the light and the dark theme at every check (e2e/axe.ts), so these tests get three minutes.
test.describe.configure({ mode: "serial", timeout: 180_000 });

const day = (offset: number) => new Date(Date.now() + offset * 86400000).toISOString().slice(0, 10);

async function scenario(page: Page) {
  const t = token();
  const me = await (await page.request.get("/api/v1/auth/me")).json();
  const hr = await post(page, "applications", { name: `${t} HR`, lifecycle: "active" });
  const done = await post(page, "renewals", {
    name: `${t} Workday`, auto_renew: "yes", renewal_date: day(60), notice_value: 1, notice_unit: "months", term_value: 1, term_unit: "years",
    applications: [hr.id], followup: me.user.id, responsible: await personId(page, "Dana Ortiz"),
  });
  await post(page, `renewals/${done.id}/cycles/${done.cycle.id}/decision`, { decision: "renew" });
  await post(page, `renewals/${done.id}/cycles/${done.cycle.id}/completion`, { completed_on: day(0), completion_evidence: "PO 1" });
  const open = await post(page, `renewals/${done.id}/cycles`, {});
  const gappy = await post(page, "renewals", { name: `${t} Gappy`, auto_renew: "unknown", explicit_deadline: day(-2) });
  return { t, hr, renewal: open, gappy };
}

test("Renewals list, form with errors, and the renewal page with each tab and dialog", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const { t, renewal, gappy } = await scenario(page);
  await page.goto(`/renewals?q=${t}`);
  await expect(page.getByRole("region", { name: "Renewals table" })).toContainText(`${t} Gappy`);
  await expectNoViolations(page, "Renewals list");
  await page.goto("/renewals/new");
  await page.getByLabel("Notice period, number").fill("2");
  await expect(page.getByRole("status").filter({ hasText: "Action deadline" })).toBeVisible();
  await page.getByRole("button", { name: "Add renewal" }).click();
  await expect(page.getByRole("alert")).toContainText("Enter a name.");
  await expectNoViolations(page, "Add renewal with errors");
  await page.getByRole("combobox", { name: "Vendor" }).click();
  await expectNoViolations(page, "Add renewal with the vendor list open");

  await page.goto(`/renewals/${gappy.id}`);
  await expect(page.getByRole("region", { name: "Gaps" })).toBeVisible();
  await expectNoViolations(page, "Renewal page with gaps, overdue");
  await page.goto(`/renewals/${renewal.id}`);
  await expect(page.getByRole("tab", { name: "Current cycle (#2)" })).toBeVisible();
  await expectNoViolations(page, "Renewal page, current cycle");
  for (const [button, dialog] of [["Edit terms", "Edit cycle 2 terms"], ["Edit covered applications", "Edit covered applications"]]) {
    await page.getByRole("button", { name: button }).click();
    await expect(page.getByRole("dialog", { name: dialog })).toBeVisible();
    await expectNoViolations(page, `${dialog} dialog`);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: button })).toBeFocused();
  }
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Archive" }).click();
  await expectNoViolations(page, "Archive renewal dialog");
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Cycle history (1)" }).click();
  await expect(page.getByRole("region", { name: "Cycle history table" })).toBeVisible();
  await expectNoViolations(page, "Renewal cycle history");
  await page.getByRole("tab", { name: "Audit activity" }).click();
  await expect(page.getByRole("region", { name: "Audit activity table" })).toBeVisible();
  await expectNoViolations(page, "Renewal audit activity");
});

test("Next cycle dialog after completion", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const t = token();
  const r = await post(page, "renewals", { name: `${t} Done`, auto_renew: "no", renewal_date: day(100), term_value: 2, term_unit: "years" });
  await post(page, `renewals/${r.id}/cycles/${r.cycle.id}/decision`, { decision: "renew" });
  await post(page, `renewals/${r.id}/cycles/${r.cycle.id}/completion`, { completed_on: day(0), completion_evidence: "Renewed" });
  await page.goto(`/renewals/${r.id}`);
  await page.getByRole("button", { name: "Establish next cycle" }).click();
  await expect(page.getByRole("dialog", { name: "Establish cycle 2" })).toBeVisible();
  await expectNoViolations(page, "Establish next cycle dialog");
});

test("Needs attention with renewal queues, the notifications menu and the snooze dialog", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const { t } = await scenario(page);
  await page.goto("/?scope=all");
  const row = page.getByRole("region", { name: /^Renewal actions \(/ }).getByRole("row", { name: new RegExp(`${t} Gappy`) });
  await expect(row).toBeVisible();
  await expectNoViolations(page, "Needs attention with renewal queues");
  await row.getByRole("button", { name: `Notifications for ${t} Gappy` }).click();
  await expect(page.getByRole("menuitem", { name: "Acknowledge" })).toBeVisible();
  await expectNoViolations(page, "Notifications menu open");
  await page.getByRole("menuitem", { name: "Snooze notifications…" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expectNoViolations(page, "Snooze dialog");
  await page.keyboard.press("Escape");
  await expect(row.getByRole("button", { name: `Notifications for ${t} Gappy` })).toBeFocused();
  await row.getByRole("button", { name: `Notifications for ${t} Gappy` }).click();
  await page.getByRole("menuitem", { name: "Acknowledge" }).click();
  await expect(row).toContainText("Acknowledged");
  await expectNoViolations(page, "Needs attention with an acknowledged row and Undo");
});

test("Organization settings, delivery log, profile and the application Renewals tab", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const { hr } = await scenario(page);
  await post(page, "e2e/run-digest", { fail: true, exhaust: true });
  await page.goto("/admin/organization");
  await expect(page.getByRole("group", { name: "Renewals and digest" })).toBeVisible();
  await expectNoViolations(page, "Organization settings");
  await page.getByLabel("Reminder thresholds (days before the deadline)").fill("7, 30");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("alert")).toContainText("largest to the smallest");
  await expectNoViolations(page, "Organization settings with an error");
  await page.goto("/admin/digests");
  await expect(page.getByRole("region", { name: "Delivery log table" })).toBeVisible();
  await expectNoViolations(page, "Delivery log with failures and the banner");
  await page.goto("/profile");
  await expect(page.getByRole("checkbox", { name: "Email me a daily digest" })).toBeVisible();
  await expectNoViolations(page, "Profile");
  await page.goto(`/applications/${hr.id}?tab=renewals`);
  await expect(page.getByRole("tabpanel")).toContainText("Workday");
  await expectNoViolations(page, "Application Renewals tab");
  await post(page, "e2e/run-digest", {});
});
