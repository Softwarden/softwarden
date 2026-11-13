import { expect, test, type Page } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

/** Renewal queues on Needs attention, snooze and acknowledge, the daily digest
 *  with coordinator fallback, and delivery failures shown to Admins and retried. Digests are read back from the e2e
 *  server's in-memory outbox; nothing is emailed. */

test.describe.configure({ mode: "serial" });

const day = (offset: number) => new Date(Date.now() + offset * 86400000).toISOString().slice(0, 10);
const section = (page: Page, name: string) => page.getByRole("region", { name: new RegExp(`^${name}`) });
type Sent = { to: string[]; subject: string; body: string };
const runDigest = (admin: Page, fail = false) => post(admin, "e2e/run-digest", { fail }) as Promise<Sent[]>;

test("Renewal actions and gaps: routing, labels, next steps; snooze keeps the row and Undo restores", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const me = await (await editor.request.get("/api/v1/auth/me")).json();
  await post(editor, "renewals", { name: `${t} Zoom Pro`, auto_renew: "yes", explicit_deadline: day(-3), followup: me.user.id, responsible: await personId(editor, "Lee Chen") });
  await post(editor, "renewals", { name: `${t} Unrouted`, auto_renew: "unknown", renewal_date: day(130) });

  await editor.goto("/");
  const actions = section(editor, "Renewal actions");
  const row = actions.getByRole("row", { name: new RegExp(`${t} Zoom Pro`) });
  await expect(row).toContainText("Entered");
  await expect(row).toContainText("Overdue by 3 days");
  await expect(row.getByRole("link", { name: `Record decision for ${t} Zoom Pro` })).toBeVisible();
  // Unrouted items are the coordinator's, so they aren't in the Editor's "Assigned to me".
  await expect(section(editor, "Renewal gaps")).not.toContainText(`${t} Unrouted`);
  await editor.goto("/?scope=unassigned");
  await expect(section(editor, "Renewal gaps").getByRole("row", { name: new RegExp(`${t} Unrouted`) })).toContainText("Assumed deadline");

  await editor.goto("/");
  await row.getByRole("button", { name: `Notifications for ${t} Zoom Pro` }).click();
  await editor.getByRole("menuitem", { name: "Snooze notifications…" }).click();
  const dialog = editor.getByRole("dialog", { name: `Snooze notifications for ${t} Zoom Pro?` });
  await dialog.getByLabel("Snooze until").fill(day(4));
  await dialog.getByRole("button", { name: "Snooze" }).click();
  await expect(editor.getByRole("status").filter({ hasText: `snoozed until ${day(4)}` })).toHaveCount(1);
  await expect(row).toContainText(`Snoozed until ${day(4)}`); // still listed, marked
  await expect(row).toContainText("Overdue by 3 days"); // the deadline is unchanged
  await editor.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(editor.getByRole("status").filter({ hasText: "Undone" })).toHaveCount(1);
  await expect(row).not.toContainText("Snoozed until");

  // The renewal page says when your notifications are snoozed.
  await row.getByRole("button", { name: `Notifications for ${t} Zoom Pro` }).click();
  await editor.getByRole("menuitem", { name: "Acknowledge" }).click();
  await expect(row).toContainText("Acknowledged");
  await row.getByRole("link", { name: `${t} Zoom Pro` }).first().click();
  await expect(editor.getByRole("note")).toContainText("You acknowledged this until its next reminder. The deadline is unchanged.");
});

test("Daily digest: one email per user, coordinator gets the unassigned, owners are never emailed, snoozes left out", async ({ browser, baseURL }) => {
  const t = token();
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const me = await (await editor.request.get("/api/v1/auth/me")).json();
  const hr = await post(editor, "applications", { name: `${t} HR`, lifecycle: "active" });
  const three = [hr.id, (await post(editor, "applications", { name: `${t} Benefits`, lifecycle: "active" })).id,
    (await post(editor, "applications", { name: `${t} Payroll`, lifecycle: "active" })).id];
  // The 30-day threshold fires today, once for the renewal however many applications it covers.
  await post(editor, "renewals", { name: `${t} Workday`, auto_renew: "yes", explicit_deadline: day(30), renewal_date: day(90), followup: me.user.id, applications: three, responsible: await personId(editor, "Dana Ortiz") });
  await post(editor, "renewals", { name: `${t} Nobody's`, auto_renew: "yes", explicit_deadline: day(-1) });
  const quiet = await post(editor, "renewals", { name: `${t} Quiet`, auto_renew: "yes", explicit_deadline: day(-2), followup: me.user.id });
  await post(editor, "needs-attention/snoozes", { subject_type: "renewal_actions", subject_id: quiet.id, kind: "snooze", until: day(5) });

  const sent = await runDigest(admin);
  const to = (email: string) => sent.filter((m) => m.to[0] === email);
  expect(sent.every((m) => m.to[0].endsWith("@example.test"))).toBeTruthy();
  expect(sent.map((m) => m.to[0])).not.toContain("dana.ortiz@example.test"); // a Person, not a User
  expect(to("editor@example.test")).toHaveLength(1);
  const mine = to("editor@example.test")[0];
  expect(mine.subject).toMatch(/^Softwarden: \d+ items? needs? attention \(/);
  expect(mine.body).toContain(`${t} Workday — action deadline ${day(30)} (Entered) — in 30 days`);
  expect(mine.body.split(`${t} Workday — action deadline`).length - 1).toBe(1);
  expect(mine.body).not.toContain(`${t} Quiet — action deadline`);
  expect(mine.body).toContain("Snoozed items are left out of this email but stay in Needs attention.");
  const coordinator = to("admin@example.test")[0];
  expect(coordinator.body).toContain("UNASSIGNED (");
  expect(coordinator.body).toContain(`Renewal actions: ${t} Nobody's — action deadline ${day(-1)} (Entered) — Overdue by 1 day`);

  // Rerun today: the 30-day reminder has fired, so the Workday action isn't repeated; the overdue one is.
  const again = await runDigest(admin);
  expect(again.find((m) => m.to[0] === "editor@example.test")?.body ?? "").not.toContain(`${t} Workday — action deadline`);
  expect(again.find((m) => m.to[0] === "admin@example.test")!.body).toContain(`${t} Nobody's — action deadline`);

  // The renewal page shows the reminder as sent.
  await editor.goto("/renewals?q=" + t);
  await editor.getByRole("link", { name: `${t} Workday` }).click();
  await expect(editor.getByRole("region", { name: "Action deadline" })).toContainText(`30 days, sent ${day(0)}`);
});

test("Digest off in the profile means no email; SMTP failure is shown to Admins and Retry now sends it", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await editor.goto("/profile");
  const box = editor.getByRole("checkbox", { name: "Email me a daily digest" });
  await box.uncheck();
  await expect(editor.getByRole("status").filter({ hasText: "Daily digest turned off." })).toHaveCount(1);
  expect((await runDigest(admin)).map((m) => m.to[0])).not.toContain("editor@example.test");
  await box.check();
  await expect(editor.getByRole("status").filter({ hasText: "Daily digest turned on." })).toHaveCount(1);

  // SMTP down: retried, then left Failed (the e2e run uses up every attempt at once).
  expect(await post(admin, "e2e/run-digest", { fail: true, exhaust: true })).toEqual([]);
  await admin.goto("/admin/digests");
  const log = admin.getByRole("region", { name: "Delivery log table" });
  await expect(log.getByRole("row", { name: /Sam Editor/ }).first()).toContainText("Failed");
  await expect(log.getByRole("row", { name: /Sam Editor/ }).first()).toContainText("4");
  await expect(log).toContainText("Couldn't connect to the SMTP server");
  // Every item is still in Needs attention while email is down.
  await editor.goto("/");
  await expect(section(editor, "Renewal actions")).toBeVisible();
  await log.getByRole("button", { name: /Retry now for Sam Editor/ }).first().click();
  await expect(admin.getByRole("status").filter({ hasText: "Digest for Sam Editor sent." })).toHaveCount(1);
});

test("Admin banner while the latest digest run has failures, linking to the delivery log", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await post(admin, "e2e/run-digest", { fail: true, exhaust: true });
  await admin.goto("/applications");
  const banner = admin.getByRole("status").filter({ hasText: "Digest email failed for" });
  await expect(banner).toContainText("Every item is still in Needs attention.");
  await banner.getByRole("link", { name: "View delivery log" }).click();
  await expect(admin).toHaveURL(/\/admin\/digests\?status=failed/);
  await expect(admin.getByRole("heading", { name: "Digest delivery log" })).toBeVisible();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  await editor.goto("/applications");
  await expect(editor.getByRole("status").filter({ hasText: "Digest email failed for" })).toHaveCount(0);
  await post(admin, "e2e/run-digest", {}); // leave a clean run for the other specs
});
