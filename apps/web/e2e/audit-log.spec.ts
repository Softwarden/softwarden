import { expect, test, type Locator, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, personId, post, token } from "./helpers";

/** The Audit log's URL filters, the event page with its DiffView and "same request" link, the History tab's
 *  diffs for Viewers, and tables that scroll inside their labelled region at narrow widths. Synthetic data only. */

/** An application created with one tag as Proposed, then edited in one request: tag swapped, lifecycle Active. */
async function editedRecord(editor: Page) {
  const t = token();
  const app = await post(editor, "applications", { name: `${t} Ledger`, lifecycle: "proposed", tags: [`${t}-old`] });
  await post(editor, `applications/${app.id}`, { lifecycle: "active", tags: [`${t}-new`] }, "PATCH");
  return { t, app };
}

const log = (page: Page) => page.getByRole("region", { name: "Audit log table" });
const diffRow = (diff: Locator, field: string) => diff.getByRole("row", { name: new RegExp(`^${field}`) });

test("Filters live in the URL, survive a reload and clear together", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { t, app } = await editedRecord(editor);
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/admin/audit");
  await admin.getByLabel("Actor").selectOption({ label: "Sam Editor" });
  await admin.getByLabel("Event").selectOption({ label: "Record changes" });
  const today = new Date().toISOString().slice(0, 10);
  const yesterday = new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
  await admin.getByLabel("From", { exact: true }).fill(yesterday);
  await expect(admin).not.toHaveURL(/from=/); // not while typing
  await admin.getByLabel("From", { exact: true }).press("Enter"); // dates apply on Enter or blur
  await admin.getByLabel("To", { exact: true }).fill(new Date(Date.now() + 86_400_000).toISOString().slice(0, 10));
  await admin.getByLabel("To", { exact: true }).press("Enter"); // dates apply on Enter or blur
  await admin.getByLabel("Target type").selectOption({ label: "Record" });
  await expect(log(admin).getByRole("link", { name: `${t} Ledger` }).first()).toBeVisible();
  await expect(admin).toHaveURL(/actor=\d+/);
  await expect(admin).toHaveURL(/action=resource/);
  await expect(admin).toHaveURL(new RegExp(`from=${yesterday}`));
  await expect(admin).toHaveURL(/target_type=Resource/);

  // A whole ID only: a partial one is refused before it reaches the URL.
  const target = admin.getByLabel("Target ID");
  await target.fill(app.id.slice(0, 8));
  await target.press("Enter");
  await expect(admin.getByText("Enter the whole ID")).toBeVisible();
  await expect(admin).not.toHaveURL(/target=/);
  await target.fill(app.id);
  await target.press("Enter");
  await expect(admin).toHaveURL(new RegExp(`target=${app.id}`));
  const rows = log(admin).getByRole("row");
  await expect(rows).toHaveCount(3); // header, Fields changed, Record created
  await expect(rows.nth(1)).toContainText("Fields changed");
  await expect(rows.nth(2)).toContainText("Record created");
  await expectNoViolations(admin, "Audit log with every filter set");

  // Reload: the same filters, the same rows.
  await admin.reload();
  await expect(admin.getByLabel("Event")).toHaveValue("resource");
  await expect(admin.getByLabel("Target ID")).toHaveValue(app.id);
  await expect(admin.getByLabel("Target type")).toHaveValue("Resource");
  await expect(log(admin).getByRole("row")).toHaveCount(3);

  // Nothing matches: say so, and offer to clear.
  await admin.getByLabel("To", { exact: true }).fill(yesterday);
  await admin.getByLabel("To", { exact: true }).press("Enter"); // dates apply on Enter or blur
  await admin.getByLabel("From", { exact: true }).fill(yesterday);
  await admin.getByLabel("From", { exact: true }).press("Enter"); // dates apply on Enter or blur
  if (today !== yesterday) await expect(admin.getByText("No events match these filters")).toBeVisible();
  await expectNoViolations(admin, "Audit log, no events match");
  await admin.getByRole("button", { name: "Clear filters" }).click();
  await expect(admin).toHaveURL(/\/admin\/audit$/);
  await expect(admin.getByLabel("Event")).toHaveValue("");
  await expect(admin.getByLabel("Target ID")).toHaveValue("");
});

test("An event's page: DiffView of a tag and lifecycle change, the same-request link, links back to the record", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { t, app } = await editedRecord(editor);
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto(`/admin/audit?target=${app.id}`);
  await log(admin).getByRole("link", { name: "Fields changed" }).click();
  await expect(admin.getByRole("heading", { name: "Fields changed", level: 1 })).toBeVisible();
  await expect(admin).toHaveURL(/\/admin\/audit\/[0-9a-f-]{36}$/);

  const diff = admin.getByRole("table", { name: /^Changes in Fields changed/ });
  const lifecycle = diffRow(diff, "Lifecycle");
  await expect(lifecycle.getByRole("cell").first()).toHaveText(/Changed/);
  await expect(lifecycle.locator('[data-side="before"]')).toHaveText("Proposed");
  await expect(lifecycle.locator('[data-side="after"]')).toHaveText("Active");
  await expect(lifecycle.locator('[data-side="after"]')).toHaveClass(/bg-state-pending/); // changed cells in the pending tone
  const tags = diffRow(diff, "Tags");
  await expect(tags).toContainText("1 added, 1 removed");
  await expect(tags.locator('[data-diff="removed"]')).toHaveText(`−${t}-old (removed)`);
  await expect(tags.locator('[data-diff="added"]')).toHaveText(`+${t}-new (added)`);
  await expect(diff.getByRole("row", { name: /^Name/ })).toHaveCount(0); // unchanged fields aren't listed
  const event = admin.getByRole("region", { name: "Event" });
  await expect(event.getByRole("link", { name: `${t} Ledger` })).toHaveAttribute("href", `/applications/${app.id}`);
  await expectNoViolations(admin, "Audit event with a DiffView");

  // Every event the edit's request wrote: here, just this one.
  await event.getByRole("link", { name: "Show every event from the same request" }).click();
  await expect(admin).toHaveURL(/\/admin\/audit\?request_id=[0-9a-f-]{36}$/);
  await expect(admin.getByLabel("Request ID")).toHaveValue(/[0-9a-f-]{36}/);
  await expect(log(admin).getByRole("row")).toHaveCount(2);
  await expect(log(admin).getByRole("row").nth(1)).toContainText("Fields changed");

  // The creation shows the values it was created with, in one column.
  await admin.goto(`/admin/audit?target=${app.id}&action=resource.created`);
  await log(admin).getByRole("link", { name: "Record created" }).click();
  await expect(admin.getByText("Created with these values; there was nothing before.")).toBeVisible();
  const created = admin.getByRole("table", { name: /^Changes in Record created/ });
  await expect(created.getByRole("columnheader", { name: "Value" })).toBeVisible();
  await expect(diffRow(created, "Lifecycle")).toContainText("Proposed");
  // The breadcrumb returns to the log as it was filtered.
  await admin.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "Audit log" }).click();
  await expect(admin).toHaveURL(new RegExp(`target=${app.id}`));
  await expect(admin.getByLabel("Event")).toHaveValue("resource.created");
});

test("Events without field detail, child events and an unknown event", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const t = token();
  const app = await post(admin, "applications", { name: `${t} Desk`, lifecycle: "active" });
  await post(admin, `applications/${app.id}/reviews`, { confirmed_by: await personId(admin, "Dana Ortiz"), evidence: "Synthetic" });
  // A sign-in keeps only its summary.
  await admin.goto("/admin/audit?action=user.login");
  await log(admin).getByRole("link", { name: "Sign-in" }).first().click();
  await expect(admin.getByTestId("diff-none")).toContainText("No field-level detail was recorded for this event.");
  await expectNoViolations(admin, "Audit event without field detail");

  // A review is written on the review row; the log and its page say which part of the record it concerns.
  await admin.goto(`/admin/audit?related_target=${app.id}`);
  const review = log(admin).getByRole("row", { name: /Review recorded/ });
  await expect(review).toContainText("On a review");
  await review.getByRole("link", { name: "Review recorded" }).click();
  const event = admin.getByRole("region", { name: "Event" });
  await expect(event.getByRole("link", { name: `The reviews of ${t} Desk` })).toHaveAttribute("href", `/applications/${app.id}?tab=history`);
  await expect(event.getByRole("link", { name: `${t} Desk`, exact: true })).toHaveAttribute("href", `/applications/${app.id}`);

  await admin.goto("/admin/audit/00000000-0000-4000-8000-000000000000");
  // The shared not-found page: a heading, and the way back to the log.
  await expect(admin.getByRole("heading", { level: 1, name: "Audit event not found" })).toBeVisible();
  await expect(admin.getByRole("main").getByRole("link", { name: "Back to Audit log" })).toBeVisible();
});

test("History tab: a Viewer opens each event's changes; child events name their part", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { t, app } = await editedRecord(editor);
  await post(editor, `applications/${app.id}/instances`, { name: `${t} Production`, environment: "production" });
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${app.id}?tab=activity`);
  const activity = viewer.getByRole("region", { name: "Audit activity table" });
  const instance = activity.getByRole("row", { name: /Instance added/ });
  await expect(instance.getByTestId("child-kind")).toHaveText("Instance"); // the child's kind and its sentence
  await expect(instance).toContainText(`Added instance '${t} Production'`);
  const edited = activity.getByRole("row", { name: /Fields changed/ });
  await edited.getByRole("button", { name: "Show changes" }).click();
  const hide = edited.getByRole("button", { name: "Hide changes" });
  await expect(hide).toHaveAttribute("aria-expanded", "true");
  await expect(hide).toHaveAccessibleDescription(/^Fields changed \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  const diff = viewer.getByRole("table", { name: /^Changes in Fields changed/ });
  await expect(diffRow(diff, "Lifecycle").locator('[data-side="after"]')).toHaveText("Active");
  await expect(diffRow(diff, "Tags").locator('[data-diff="added"]')).toContainText(`${t}-new`);
  await expect(viewer.getByRole("region", { name: /^Changes in/ })).toHaveCount(0); // no extra tab stop inside the History table
  await expectNoViolations(viewer, "History tab with an event's changes open");
  await hide.click();
  await expect(diff).toHaveCount(0);

  // The log itself stays Admin-only, with a refusal that says so.
  await viewer.goto("/admin/audit");
  await expect(viewer.getByTestId("admin-only")).toContainText("Only Admins can open Audit log.");
  await expect(viewer.getByRole("region", { name: "Audit log table" })).toHaveCount(0);
});

/** The labelled, focusable region scrolls, not a box inside it, and the page itself never scrolls sideways. */
async function expectRegionScrolls(page: Page, name: string) {
  const region = page.getByRole("region", { name });
  await expect(region).toBeVisible();
  const box = await region.evaluate((el) => {
    const inner = el.querySelector<HTMLElement>('[data-slot="table-container"]');
    return {
      overflows: el.scrollWidth > el.clientWidth,
      // ui/Table's own wrapper must not be a scroll box (it would take the scrolling, and can't take focus everywhere).
      innerScrolls: inner ? getComputedStyle(inner).overflowX !== "visible" : false,
      pageOverflows: document.documentElement.scrollWidth > window.innerWidth,
    };
  });
  expect(box, `${name}: the region scrolls, the table container doesn't, the page doesn't`).toEqual({ overflows: true, innerScrolls: false, pageOverflows: false });
  await region.focus();
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowRight"); // keyboard users scroll the focused region
  await expect.poll(() => region.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
}

test("Narrow screens: the audit log, users and queue tables scroll inside their labeled region", async ({ browser, baseURL }) => {
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const { t } = await editedRecord(editor);
  await post(editor, "applications", { name: `${t} Ownerless`, lifecycle: "active", criticality: "high" });
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.setViewportSize({ width: 400, height: 800 });
  await admin.goto("/admin/audit");
  await expect(log(admin).getByRole("row").nth(1)).toBeVisible();
  await expectRegionScrolls(admin, "Audit log table");
  await admin.goto("/admin/users");
  await expect(admin.getByRole("region", { name: "Users table" }).getByRole("row").nth(1)).toBeVisible();
  await expectRegionScrolls(admin, "Users table");
  // Queue tables stack below 640 px; between 640 and 1023 px they keep their columns and scroll.
  await admin.setViewportSize({ width: 700, height: 800 });
  await admin.goto("/?scope=all");
  await expect(admin.getByRole("region", { name: "Queue table: Ownership gaps" }).getByRole("row").nth(1)).toBeVisible();
  await expectRegionScrolls(admin, "Queue table: Ownership gaps");
  await expectNoViolations(admin, "Needs attention at 700 px");
});
