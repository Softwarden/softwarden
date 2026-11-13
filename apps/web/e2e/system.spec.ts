import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post } from "./helpers";

/** The System status page ("the System page shows each schedule's last run and state").
 *  The e2e server runs no worker, so the test-only `e2e/system-states` endpoint (api/e2e_views.py) puts the
 *  installation-wide heartbeat and schedule rows into known states first. Those rows are shared by the whole e2e
 *  database, so these tests run one after another, and nothing else in the suite reads them. */
test.describe.configure({ mode: "serial" });

type States = { worker?: "ok" | "stale" | "never"; schedules?: Record<string, string>; retired?: string[]; jobs?: boolean };
const setStates = (admin: Page, states: States) => post(admin, "e2e/system-states", states);

const LABELS: Record<string, string> = {
  "jobs.prune": "Delete finished jobs after 30 days",
  "discovery.connectors": "Queue requested and daily discovery connector runs; fail runs that lost their lease",
  "imports.purge": "Purge expired import files",
  "digest.build": "Build daily digests",
  "notify.deliveries": "Fail deliveries whose worker stopped mid-send; requeue deliveries whose job is gone",
};
const scheduleRow = (page: Page, name: string) => page.getByRole("region", { name: "Schedules table" }).getByRole("row", { name: new RegExp(LABELS[name] ?? name) });

test("Before the worker has run: never seen, every schedule never run, unhealthy", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await setStates(admin, { worker: "never" });
  await admin.goto("/admin/system");
  await expect(admin.getByRole("heading", { name: "System status", level: 1 })).toBeVisible();
  const overall = admin.getByTestId("overall-status");
  await expect(overall).toContainText("Unhealthy");
  await expect(overall).toContainText("No worker has ticked against this database yet.");
  const worker = admin.getByRole("region", { name: "Worker" });
  await expect(worker).toContainText("Never seen");
  await expect(worker).toContainText("Start the worker");
  for (const name of Object.keys(LABELS)) {
    const row = scheduleRow(admin, name);
    await expect(row).toContainText("Never run");
    await expect(row).toContainText(name);
  }
  // Cron in words, with the cron itself.
  await expect(scheduleRow(admin, "jobs.prune")).toContainText("Daily at 03:40 UTC");
  await expect(scheduleRow(admin, "notify.deliveries")).toContainText("Every minute");
  await expect(admin.getByRole("region", { name: "Job counts table" }).getByRole("row", { name: /^Queued/ })).toBeVisible();
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status before the worker has run");
});

test("Each schedule state, a stale worker, retired rows and recent job problems", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await setStates(admin, {
    worker: "stale",
    schedules: { "jobs.prune": "ok", "discovery.connectors": "failing", "imports.purge": "stuck", "digest.build": "overdue", "notify.deliveries": "disabled" },
    retired: ["e2e.retired"],
    jobs: true,
  });
  await admin.goto("/admin/system");
  const overall = admin.getByTestId("overall-status");
  await expect(overall).toContainText("Unhealthy");
  await expect(overall).toContainText(/The worker's last tick was 10 minutes ago/);
  await expect(overall).toContainText(`Stuck: ${LABELS["imports.purge"]}.`);
  await expect(overall).toContainText(`Overdue: ${LABELS["digest.build"]}.`);
  await expect(overall).toContainText(`Last run failed: ${LABELS["discovery.connectors"]}.`);
  await expect(admin.getByRole("region", { name: "Worker" })).toContainText("Stale");

  await expect(scheduleRow(admin, "jobs.prune")).toContainText("OK");
  await expect(scheduleRow(admin, "jobs.prune")).toContainText("minutes ago"); // last started, finished, succeeded
  const failing = scheduleRow(admin, "discovery.connectors");
  await expect(failing).toContainText("Failing");
  await expect(failing).toContainText("SMTPException"); // the error type only, as the API gives it
  await expect(failing.getByText("SMTPException")).toHaveCount(1); // in Last error, not repeated under the state
  await expect(scheduleRow(admin, "imports.purge")).toContainText("Stuck");
  await expect(scheduleRow(admin, "imports.purge")).toContainText("no outcome before its lease expired");
  await expect(scheduleRow(admin, "digest.build")).toContainText("Overdue");
  const disabled = scheduleRow(admin, "notify.deliveries");
  await expect(disabled).toContainText("Disabled");
  await expect(disabled.getByRole("cell", { name: "No", exact: true })).toBeVisible(); // Enabled
  await expect(scheduleRow(admin, "e2e.retired")).toContainText("Retired");

  const problems = admin.getByRole("region", { name: "Recent problems table" });
  await expect(problems.getByRole("row", { name: /Failed/ })).toContainText("This organization");
  await expect(problems.getByRole("row", { name: /Failed/ })).toContainText("ConnectionError");
  await expect(problems.getByRole("row", { name: /Abandoned/ })).toContainText("Installation-wide");
  const counts = admin.getByRole("region", { name: "Job counts table" });
  await expect(counts.getByRole("row", { name: /^Failed/ }).getByRole("cell").first()).not.toHaveText("0");
  await expect(counts.getByRole("row", { name: /^Abandoned/ }).getByRole("cell").nth(1)).not.toHaveText("0");
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status with every problem state");
});

test("Running, never run and failing make it degraded; all on time is OK; Check now refreshes", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await setStates(admin, { worker: "ok", schedules: { "jobs.prune": "running", "discovery.connectors": "failing", "imports.purge": "never_run", "digest.build": "ok", "notify.deliveries": "ok" } });
  await admin.goto("/admin/system");
  const overall = admin.getByTestId("overall-status");
  await expect(overall).toContainText("Degraded");
  await expect(admin.getByRole("region", { name: "Worker" })).toContainText("Running");
  await expect(scheduleRow(admin, "jobs.prune")).toContainText("Running since");
  await expect(scheduleRow(admin, "imports.purge")).toContainText("Never run");
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status, degraded");

  await setStates(admin, { worker: "ok", schedules: Object.fromEntries(Object.keys(LABELS).map((n) => [n, "ok"])) });
  await admin.getByRole("button", { name: "Check now" }).click();
  await expect(overall).toContainText("OK");
  await expect(overall).toContainText("no schedule is stuck, overdue or failing");
});

test("Refetches every 30 seconds while the page is open", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await setStates(admin, { worker: "never" });
  await admin.clock.install();
  await admin.goto("/admin/system");
  const worker = admin.getByRole("region", { name: "Worker" });
  await expect(worker).toContainText("Never seen");
  await setStates(admin, { worker: "ok" });
  await admin.clock.runFor(31_000);
  await expect(worker).toContainText("Running");
});

test("Loading and error states; Retry recovers", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await setStates(admin, { worker: "ok" });
  let fail = true;
  let release: () => void = () => undefined;
  const held = new Promise<void>((done) => { release = done; });
  await admin.route("**/api/v1/system", async (route) => {
    await held;
    if (fail) await route.fulfill({ status: 500, contentType: "application/problem+json", body: JSON.stringify({ title: "Server error", status: 500, detail: "The server had a problem." }) });
    else await route.continue();
  });
  await admin.goto("/admin/system");
  await expect(admin.getByText("Loading system status…")).toBeVisible();
  release();
  const alert = admin.getByRole("alert");
  await expect(alert).toContainText("Nothing was changed.");
  await expectNoViolations(admin, "System status, error");
  fail = false;
  await alert.getByRole("button", { name: "Retry" }).click();
  await expect(admin.getByRole("region", { name: "Worker" })).toContainText("Running");
});

test("Admins reach it from the sidebar and with g y; other roles get a clear refusal", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/");
  await expect(admin.getByRole("heading", { name: "Needs attention", level: 1 })).toBeVisible();
  await expect(admin.getByRole("navigation", { name: "Primary" }).getByRole("group", { name: "Admin" }).getByRole("link", { name: "System status" })).toBeVisible();
  await admin.keyboard.press("g");
  await admin.keyboard.press("y");
  await expect(admin).toHaveURL(/\/admin\/system$/);
  await expect(admin.getByRole("heading", { name: "System status", level: 1 })).toBeVisible();

  for (const email of ["viewer@example.test", "editor@example.test"]) {
    const other = await pageAs(browser, baseURL, email);
    await other.goto("/admin/system");
    await expect(other.getByRole("heading", { name: "System status", level: 1 })).toBeVisible();
    await expect(other.getByTestId("admin-only")).toContainText("Only Admins can open System status.");
    await expect(other.getByRole("link", { name: "System status" })).toHaveCount(0);
    await expect(other.getByRole("region", { name: "Worker" })).toHaveCount(0);
    if (email.startsWith("viewer")) await expectNoViolations(other, "System status refused to a Viewer");
  }
});

// ---- Backups --------------------------------------------------------------------------------------------
// The e2e API server takes no backups: SOFTWARDEN_BACKUP_DIR is empty and there is no secrets volume, so its real state
// is "not configured" with no warnings, and acknowledging the key is refused (409, no key yet). There is no test-only
// hook for the backup state (BackupState), so the other states are the real `GET system` answer with its `backup` and
// `warnings` replaced in the browser (the shapes are the API's: core/system_views.BackupStatusSerializer).

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();
type Backup = Record<string, unknown>;
type Warning = { code: string; message: string };
const GOOD: Backup = {
  state: "ok", destination: "/data/backups", cron: "30 2 * * *", last_success_at: hoursAgo(3), age_seconds: 3 * 3600, stale_after_seconds: 26 * 3600,
  last_set: "softwarden-20261107T023000Z", last_size_bytes: 50 * 1024 * 1024, last_duration_seconds: 42, last_failure_at: null, last_failure: "",
  running: false, key_source: "generated", key_present: true, key_acknowledged: true, key_acknowledged_at: hoursAgo(48),
};

/** Serve `GET system` with the backup block and warnings `state()` gives at the time of each request. */
async function mockBackup(page: Page, state: () => { backup: Backup; warnings: Warning[] }) {
  await page.route((url) => url.pathname === "/api/v1/system", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const body = await response.json() as { backup: Backup };
    const { backup, warnings } = state();
    await route.fulfill({ response, json: { ...body, backup: { ...body.backup, ...backup }, warnings } });
  });
}
const backups = (page: Page) => page.getByRole("region", { name: "Backups" });
const warningItem = (page: Page, code: string) => backups(page).getByRole("list", { name: "Backup warnings" }).locator(`li[data-code="${code}"]`);

test("Backups in the e2e installation: not configured, no warnings, the backup folder as a named gap", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await setStates(admin, { worker: "ok", schedules: Object.fromEntries(Object.keys(LABELS).map((n) => [n, "ok"])) });
  await admin.goto("/admin/system");
  const panel = backups(admin);
  await expect(panel.getByTestId("backup-state")).toContainText("○Not configured");
  await expect(panel).toContainText("SOFTWARDEN_BACKUP_DIR is empty");
  await expect(panel).toContainText("ops/backup.sh");
  await expect(panel.locator("[data-gap]", { hasText: "No backup folder" })).toBeVisible();
  await expect(panel.getByTestId("backup-warnings")).toHaveCount(0);
  await expect(panel).toContainText("Daily at 02:30 UTC");
  await expect(admin.getByTestId("overall-status")).not.toContainText("Backups:");
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status, backups not configured");
});

test("A good backup shows its age, set, size, destination, schedule and the confirmed key", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await mockBackup(admin, () => ({ backup: GOOD, warnings: [] }));
  await admin.goto("/admin/system");
  const panel = backups(admin);
  await expect(panel.getByTestId("backup-state")).toContainText("✓Backed up 3 hours ago");
  await expect(panel).toContainText("softwarden-20261107T023000Z");
  await expect(panel).toContainText("50 MB");
  await expect(panel).toContainText("took 42 seconds");
  await expect(panel).toContainText("/data/backups");
  await expect(panel).toContainText("None recorded"); // last failure
  await expect(panel).toContainText("✓Copy confirmed off the host");
  await expect(panel.getByTestId("backup-warnings")).toHaveCount(0);
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status, a good backup");
});

test("Stored secrets under a credential key that isn't configured: the warning names the off-host key copy", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await mockBackup(admin, () => ({
    backup: GOOD,
    warnings: [{ code: "stored_secrets_unreadable", message: "Some stored secrets were encrypted under a credential key this installation doesn't have, so they can't be read." }],
  }));
  await admin.goto("/admin/system");
  // Not a backup warning: it has its own "To fix" panel, and the backup panel lists none.
  const item = admin.getByRole("region", { name: "To fix" }).getByRole("list", { name: "Warnings" }).locator('li[data-code="stored_secrets_unreadable"]');
  await expect(item).toContainText("⊘Secrets unreadable");
  await expect(item).toContainText("can't be read.");
  await expect(item).toContainText("SOFTWARDEN_CREDENTIAL_KEYS");
  await expect(item).toContainText("credential_keys");
  await expect(backups(admin).getByTestId("backup-warnings")).toHaveCount(0);
  await expect(admin.getByTestId("overall-status")).toContainText("1 warning to fix (below). Warnings don't change the overall status.");
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status, stored secrets unreadable");
});

test("An Admin of one organization among others gets the organization view: its own jobs and its own secrets warning, no schedules or backups", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await setStates(admin, { worker: "ok", schedules: Object.fromEntries(Object.keys(LABELS).map((n) => [n, "ok"])) });
  // The e2e Admin's organization is the installation's first, so the API gives it the installation view; the
  // organization view is what any other organization's Admin gets (core/system_views.organization_status), sent as is.
  const counts = { queued: 0, running: 0, succeeded: 4, failed: 1, abandoned: 0 };
  await admin.route((url) => url.pathname === "/api/v1/system", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const body = await response.json() as { status: string; checked_at: string; worker: unknown };
    await route.fulfill({
      response,
      json: {
        scope: "organization", status: "degraded", checked_at: body.checked_at, worker: body.worker,
        jobs: { organization: counts, recent_problems: [] },
        warnings: [{ code: "stored_secrets_unreadable", message: "Some stored secrets were encrypted under a credential key this installation doesn't have, so they can't be read." }],
      },
    });
  });
  await admin.goto("/admin/system");
  await expect(admin.getByRole("heading", { level: 1, name: "System status" })).toBeVisible();
  // Self-hosted (the e2e server): the page says who sees the installation.
  await expect(admin.getByText("The installation's schedules and backups are its operator's: platform staff see them here, and so do the Admins of the installation's first organization unless it is on the Cloud (billed) edition.")).toBeVisible();
  const overall = admin.getByTestId("overall-status");
  await expect(overall).toContainText("Degraded");
  await expect(overall).toContainText("One of the installation's schedules is behind or failing. Its operator sees which and fixes it");
  await expect(overall).not.toContainText("no schedule is stuck");
  await expect(admin.getByTestId("organization-view")).toContainText("this page shows your organization's part only: no schedules, backups or other organizations' jobs.");
  await expect(admin.getByRole("region", { name: /^Schedules(?! table)/ })).toHaveCount(0);
  await expect(admin.getByRole("region", { name: "Backups" })).toHaveCount(0);
  // Its own stored secrets, with what an organization's Admin can do about them (not the operator's commands).
  const item = admin.getByRole("region", { name: "To fix" }).locator('li[data-code="stored_secrets_unreadable"]');
  await expect(item).toContainText("⊘Secrets unreadable");
  await expect(item).toContainText("Enter those secrets again where they are set");
  await expect(item).not.toContainText("SOFTWARDEN_CREDENTIAL_KEYS");
  // Its own job counts only.
  const jobs = admin.getByRole("region", { name: "Job counts table" });
  await expect(jobs.getByRole("columnheader")).toHaveText(["Status", "This organization"]);
  await expect(jobs.getByRole("row", { name: /^Succeeded/ })).toContainText("4");
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status, organization view");
});

test("A failed and stale backup with a missing key: each warning with its fix; the failure's stage message; then stale alone", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  let failed = true;
  await mockBackup(admin, () => failed
    ? {
      backup: { ...GOOD, state: "stale", last_success_at: hoursAgo(30), age_seconds: 30 * 3600, last_failure_at: hoursAgo(5), last_failure: "The backup failed while dumping the database: pg_dump exited with status 1.", key_present: false, key_acknowledged: false },
      warnings: [
        { code: "backup_stale", message: "The last good backup is 30 hours old." },
        { code: "backup_failed", message: "The last backup failed: The backup failed while dumping the database: pg_dump exited with status 1." },
        { code: "backup_key_missing", message: "The backup key file is missing, so backups fail." },
      ],
    }
    : { backup: { ...GOOD, state: "stale", last_success_at: hoursAgo(30), age_seconds: 30 * 3600, last_failure_at: hoursAgo(40), last_failure: "The backup failed while checking free space (BackupError)." }, warnings: [{ code: "backup_stale", message: "The last good backup is 30 hours old." }] });
  await admin.goto("/admin/system");
  const panel = backups(admin);
  const state = panel.getByTestId("backup-state");
  await expect(state).toContainText("⊘Failed"); // a failure since the last good set wins over its age
  await expect(state).toContainText("pg_dump exited with status 1.");
  await expect(panel.getByRole("heading", { name: "To fix (3)" })).toBeVisible();
  await expect(warningItem(admin, "backup_stale")).toContainText("!Backup stale");
  await expect(warningItem(admin, "backup_stale")).toContainText("The last good backup is 30 hours old.");
  await expect(warningItem(admin, "backup_stale")).toContainText("docker compose exec worker python manage.py backup_now --inline");
  await expect(warningItem(admin, "backup_failed")).toContainText("⊘Backup failed");
  await expect(warningItem(admin, "backup_failed")).toContainText("docker compose logs worker | grep backup");
  await expect(warningItem(admin, "backup_key_missing")).toContainText("!No backup key");
  await expect(warningItem(admin, "backup_key_missing")).toContainText("SOFTWARDEN_BACKUP_KEY_FILE");
  await expect(panel.locator("[data-gap]", { hasText: "No backup key file" })).toBeVisible();
  await expect(panel.getByRole("button", { name: /I have copied the backup key/ })).toHaveCount(0);
  await expect(admin.getByTestId("overall-status")).toContainText("3 warnings to fix (below). Warnings don't change the overall status.");
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status, backup failed, stale, no key");

  failed = false;
  await admin.getByRole("button", { name: "Check now" }).click();
  await expect(state).toContainText("!Stale");
  await expect(state).toContainText("The last good backup is 30 hours old (stale after 26 hours).");
  await expect(panel).toContainText("A good backup has been taken since."); // the older failure, kept for reference
  await expect(panel.getByRole("heading", { name: "To fix (1)" })).toBeVisible();
});

test("Never backed up with the generated key unconfirmed: confirming is behind a dialog, a 409 is said in words, then it is confirmed", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  let acknowledged = false;
  const never: Backup = { ...GOOD, state: "never", last_success_at: null, age_seconds: null, last_set: "", last_size_bytes: null, last_duration_seconds: null, key_acknowledged: false, key_acknowledged_at: null };
  const stale: Warning = { code: "backup_stale", message: "No backup has completed yet." };
  await mockBackup(admin, () => acknowledged
    ? { backup: { ...never, key_acknowledged: true, key_acknowledged_at: new Date().toISOString() }, warnings: [stale] }
    : { backup: never, warnings: [stale, { code: "backup_key_unacknowledged", message: "Backup key is stored on this host: copy it somewhere safe." }] });
  await admin.goto("/admin/system");
  const panel = backups(admin);
  await expect(panel.getByTestId("backup-state")).toContainText("○No backup yet");
  await expect(panel).toContainText("None yet");
  await expect(warningItem(admin, "backup_stale")).toContainText("◐No backup yet");
  await expect(warningItem(admin, "backup_stale")).toContainText("Take one now");
  const key = warningItem(admin, "backup_key_unacknowledged");
  await expect(key).toContainText("◐Key only on this host");
  await expect(key).toContainText("docker compose cp worker:/data/secrets/backup_key ./backup.key");
  await expect(panel).toContainText("◐Not confirmed copied off the host");
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "System status, key not confirmed");

  // The e2e server has no key at all, so the real API refuses (409): said in the dialog, nothing changed, only Done left.
  const opener = key.getByRole("button", { name: "I have copied the backup key somewhere safe…" });
  await opener.click();
  const dialog = admin.getByRole("dialog", { name: "Confirm the backup key is copied" });
  await expect(dialog).toContainText("recorded in the audit log of every organization");
  await expectNoViolations(admin, "Confirm the backup key dialog");
  await dialog.getByRole("button", { name: "Confirm the key is copied" }).click();
  await expect(dialog.getByRole("alert")).toContainText("There is no backup key yet.");
  await expect(dialog.getByRole("alert")).toContainText("Nothing was changed");
  await expect(dialog.getByRole("button", { name: "Confirm the key is copied" })).toHaveCount(0);
  await expectNoViolations(admin, "Confirm the backup key refused (409)");
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();

  // Where a generated key exists, the API answers 200 with the backup block; the warning goes and focus lands on the
  // key's new state, since the button that opened the dialog is gone.
  // The dialog closes on the API's answer, not after the status reloads (it waited for that reload and stayed open for
  // seconds on a busy server): here the reload is held until the dialog has closed.
  await admin.route((url) => url.pathname === "/api/v1/system/backup/acknowledge-key", async (route) => {
    acknowledged = true;
    await route.fulfill({ status: 200, json: { ...never, key_acknowledged: true, key_acknowledged_at: new Date().toISOString() } });
  });
  let reload = () => {};
  const reloading = new Promise<void>((r) => { reload = r; });
  const status = (url: URL) => url.pathname === "/api/v1/system";
  await admin.route(status, async (route) => {
    if (acknowledged) await reloading;
    await route.fallback();
  });
  await opener.click();
  await dialog.getByRole("button", { name: "Confirm the key is copied" }).click();
  await expect(dialog).toHaveCount(0);
  reload();
  await expect(admin.getByRole("status").filter({ hasText: "The backup key is confirmed as copied off the host." })).toBeAttached();
  await expect(warningItem(admin, "backup_key_unacknowledged")).toHaveCount(0);
  await expect(panel).toContainText("✓Copy confirmed off the host");
  await expect(admin.locator("#backup-key-state")).toBeFocused();
  await expectNoViolations(admin, "System status, key confirmed");
});
