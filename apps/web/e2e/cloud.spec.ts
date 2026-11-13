import { expect, test, type Browser, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { CLOUD, CLOUD_ORG_SLUG, CLOUD_WEB, PLATFORM_EMAIL, STAFF_EMAIL } from "./cloud-server";
import { PASSWORD, post } from "./helpers";

/** Cloud edition journeys, against the hosted installation in cloud-server.ts: Contoso on the cloud
 *  edition, platform@example.test platform staff with no membership and editor@example.test staff with one, public
 *  signup on, and the test-only routes (the email outbox, expiring an impersonation, running export jobs). They run in
 *  order in one worker, because each changes that installation's one organization (an export made, a deletion
 *  scheduled and cancelled, overrides set and removed, impersonations). axe runs in light and dark at each new page and
 *  dialog (e2e/axe.ts).
 *
 *  Sessions are made here rather than with helpers.pageAs: that caches sign-ins per email for the main server, and
 *  cookies don't tell ports apart, so a main-server session would be sent to this one. */

test.describe.configure({ mode: "serial" });
test.skip(!CLOUD, "The cloud e2e server runs only with E2E_CLOUD=1 (playwright.config.ts).");
test.use({ baseURL: CLOUD_WEB });

const ADMIN = "admin@example.test";
const VIEWER = "viewer@example.test";

/** A new browser context signed in as `email`; members land on the Dashboard, staff with no membership on the console's
 *  Organizations. */
async function signedIn(browser: Browser, email: string, landing = email === PLATFORM_EMAIL ? "Organizations" : "Dashboard"): Promise<Page> {
  const page = await (await browser.newContext({ baseURL: CLOUD_WEB })).newPage();
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: landing })).toBeVisible();
  return page;
}

/** The e2e server's outbox (POST e2e/outbox, Admin): the messages to `to`, taken out of the outbox. */
async function mailTo(admin: Page, to: string): Promise<{ to: string[]; subject: string; body: string }[]> {
  return post(admin, "e2e/outbox", { to, clear: true });
}

/** Contoso's id and a member's user id, from the console's API (a staff session). */
async function contosoMember(staff: Page, email: string) {
  const orgs: { results: { id: string; slug: string }[] } = await (await staff.request.get(`/api/v1/platform/organizations?q=${CLOUD_ORG_SLUG}`)).json();
  const orgId = orgs.results.find((o) => o.slug === CLOUD_ORG_SLUG)!.id;
  const detail: { members: { id: number; email: string }[] } = await (await staff.request.get(`/api/v1/platform/organizations/${orgId}`)).json();
  return { orgId, userId: detail.members.find((m) => m.email === email)!.id };
}

/** Whether Contoso has an application of this name (read as its Admin, through the API). */
async function applicationNamed(admin: Page, name: string) {
  const body = await (await admin.request.get(`/api/v1/applications?q=${encodeURIComponent(name)}`)).json();
  const rows: { name: string }[] = Array.isArray(body) ? body : body.results;
  return rows.some((r) => r.name === name);
}

async function openAccountMenu(page: Page, item: string) {
  await page.getByRole("button", { name: /\. Account$/ }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

test("Signup: the form, check your email, a new link waits a minute before it can be sent, and links that can't be used", async ({ page }) => {
  const stamp = Date.now();
  await page.goto("/login");
  await page.getByRole("link", { name: "Create an account" }).click();
  await expect(page).toHaveURL(/\/signup$/);
  await expect(page.getByRole("heading", { level: 1, name: "Create your Softwarden account" })).toBeVisible();
  await expectNoViolations(page, "Signup form");

  // An empty submit names every required field and focuses the first.
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByLabel("Work email (required)")).toBeFocused();
  await expect(page.getByLabel("Work email (required)")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("alert").filter({ hasText: "Enter your organization's name." })).toBeVisible();

  // The API's own field errors come back inline (a password that is too short).
  await page.getByLabel("Work email (required)").fill(`new.${stamp}@signup.example.test`);
  await page.getByLabel("Your name (required)").fill("Morgan Signup");
  await page.getByLabel("Organization name (required)").fill(`Fabrikam ${stamp} (synthetic)`);
  await page.getByLabel("Password (required)").fill("short");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByLabel("Password (required)")).toHaveAttribute("aria-invalid", "true");

  await page.getByLabel("Password (required)").fill("signup-journey-passphrase-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Check your email" })).toBeVisible();
  await expect(page.getByTestId("signup-sent")).toContainText("Check your email.");
  await expect(page.getByTestId("signup-sent")).toBeFocused();
  await expect(page.getByText(`new.${stamp}@signup.example.test`)).toBeVisible();
  await expectNoViolations(page, "Signup, check your email");
  // The API sends one message a minute per address: asking again at once says how long to wait.
  await page.getByRole("button", { name: "Send a new link" }).click();
  await expect(page.getByRole("status").filter({ hasText: /You can send a new link in \d+ seconds\./ })).toBeVisible();

  // A link that can't be used: the token leaves the address bar at once, and the answer is plain.
  await page.goto("/signup/verify#not-a-real-signup-token-0000000000");
  await expect(page.getByRole("heading", { level: 1, name: "Confirm your email" })).toBeVisible();
  await expect(page).toHaveURL(/\/signup\/verify$/);
  await expectNoViolations(page, "Confirm your email");
  await page.getByRole("button", { name: "Confirm and sign in" }).click();
  await expect(page.getByLabel("Password (required)")).toBeFocused(); // asked for the password first
  await page.getByLabel("Password (required)").fill("signup-journey-passphrase-2026");
  await page.getByRole("button", { name: "Confirm and sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "This link can't be used" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("This confirmation link isn't valid any more.");
  await expectNoViolations(page, "Signup link that can't be used");
  // Opened with no token at all.
  await page.goto("/signup/verify");
  await expect(page.getByRole("heading", { level: 1, name: "This link can't be used" })).toBeVisible();
  await expect(page.getByText("This page needs the link from your confirmation email.")).toBeVisible();
});

test("Signup end to end: the emailed link signs the new Admin in, who invites a colleague who accepts the emailed invitation", async ({ browser }) => {
  const stamp = Date.now();
  const email = `owner.${stamp}@signup.example.test`;
  const orgName = `Northwind ${stamp} (synthetic)`;
  const passphrase = "signup-owner-passphrase-2026";
  const page = await (await browser.newContext({ baseURL: CLOUD_WEB })).newPage();
  await page.goto("/signup");
  await page.getByLabel("Work email (required)").fill(email);
  await page.getByLabel("Your name (required)").fill("Robin Owner");
  await page.getByLabel("Organization name (required)").fill(orgName);
  await page.getByLabel("Password (required)").fill(passphrase);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Check your email" })).toBeVisible();

  // The confirmation email, read back from the e2e server's outbox by a seed Admin.
  const seedAdmin = await signedIn(browser, ADMIN);
  const [confirmation] = await mailTo(seedAdmin, email);
  await seedAdmin.context().close();
  const token = confirmation.body.match(/\/signup\/verify#([\w-]+)/)![1];
  await page.goto(`/signup/verify#${token}`);
  await expect(page).toHaveURL(/\/signup\/verify$/); // read, then out of the address bar
  await page.getByLabel("Password (required)").fill(passphrase);
  await page.getByRole("button", { name: "Confirm and sign in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("banner").getByText(orgName)).toBeVisible();
  await expect(page.getByRole("button", { name: "Robin Owner · Admin. Account" })).toBeVisible();
  await expectNoViolations(page, "Dashboard of a new organization after signup");

  // The new Admin invites a colleague; the invitation is emailed, and the colleague accepts it in their own browser.
  const colleague = `colleague.${stamp}@signup.example.test`;
  await page.goto("/admin/users");
  await page.getByRole("button", { name: "Invite user" }).click();
  const invite = page.getByRole("dialog", { name: "Invite user" });
  await invite.getByLabel("Email (required)").fill(colleague);
  await invite.getByLabel("Name").fill("Casey Colleague");
  await invite.getByRole("button", { name: "Invite" }).click();
  const shown = page.getByRole("dialog", { name: "Invitation emailed" });
  await expect(shown.getByTestId("invite-note")).toContainText(`Emailed to ${colleague}.`);
  await shown.getByRole("button", { name: "Done" }).click();
  const [invitation] = await mailTo(page, colleague);
  const link = invitation.body.match(/\/invite#[\w-]+/)![0];
  const other = await (await browser.newContext({ baseURL: CLOUD_WEB })).newPage();
  await other.goto(link);
  await expect(other.getByRole("heading", { level: 1, name: `Join ${orgName}` })).toBeVisible();
  await other.getByLabel("Password (required)", { exact: true }).fill("colleague-passphrase-2026");
  await other.getByLabel("Confirm password (required)").fill("colleague-passphrase-2026");
  await other.getByRole("button", { name: `Create account and join ${orgName}` }).click();
  await expect(other.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(other.getByRole("banner").getByText(orgName)).toBeVisible();
  await other.context().close();
  await page.reload();
  await expect(page.getByRole("rowheader").filter({ hasText: colleague })).toContainText("Casey Colleague");
  await page.context().close();
});

test("Usage and limits: every member sees the meters, with the usage of the maximum in words", async ({ browser }) => {
  const viewer = await signedIn(browser, VIEWER);
  await openAccountMenu(viewer, "Usage and limits");
  await expect(viewer.getByRole("heading", { level: 1, name: "Usage and limits" })).toBeVisible();
  const records = viewer.getByTestId("limit-records");
  await expect(records).toContainText("Catalog records");
  await expect(records).toContainText("0 of 5,000 records used");
  await expect(records).toContainText("✓Room left");
  await expect(viewer.getByTestId("limit-members")).toContainText("3 of 50 members used");
  await expect(viewer.getByTestId("limit-attachment_bytes")).toContainText("0 bytes of 5 GB used");
  await expect(viewer.getByTestId("limit-api_rate_read")).toContainText("1,200 requests per minute at most");
  await expectNoViolations(viewer, "Usage and limits (Viewer)");
  await viewer.context().close();
});

test("Export all data: an Admin asks for one and sees it queued, and a second is refused while it is prepared", async ({ browser }) => {
  const admin = await signedIn(browser, ADMIN);
  await admin.goto("/admin/organization");
  const section = admin.getByRole("region", { name: "Export all data" });
  await expect(section.getByText("No exports yet")).toBeVisible();
  await section.getByRole("button", { name: "Export all data" }).click();
  await expect(admin.getByRole("status").filter({ hasText: "Export requested." })).toBeAttached();
  const table = section.getByRole("region", { name: "Exports table" });
  const row = table.getByRole("row").filter({ hasText: "Export on request" });
  await expect(row).toContainText("○Queued");
  await expect(row).toContainText("by Alex Admin");
  await expect(row).toContainText("Not ready yet");
  await expect(section.getByText("Checking for the finished export every few seconds.")).toBeVisible();
  await expectNoViolations(admin, "Organization settings with an export queued");
  await section.getByRole("button", { name: "Export all data" }).click();
  await expect(section.getByRole("alert")).toContainText("An export is already being prepared.");

  // The worker builds it (here the test-only route runs the queued export jobs); the list polls, then offers it.
  await post(admin, "e2e/run-export-jobs", {});
  await expect(row).toContainText("✓Ready", { timeout: 15_000 });
  const download = row.getByRole("link", { name: /^Download/ });
  await expect(download).toBeVisible();
  await expect(section.getByText("Checking for the finished export every few seconds.")).toHaveCount(0);
  await expectNoViolations(admin, "Organization settings with an export ready");
  const zip = await admin.request.get((await download.getAttribute("href"))!);
  expect(zip.status()).toBe(200);
  expect(zip.headers()["content-type"]).toContain("application/zip");
  await admin.context().close();
});

test("Deletion: an Admin schedules it with the slug and password, every member sees the banner, and it is canceled", async ({ browser }) => {
  const admin = await signedIn(browser, ADMIN);
  await admin.goto("/admin/organization");
  const section = admin.getByRole("region", { name: "Delete the organization" });
  await expect(section).toContainText("in 30 days");
  const opener = section.getByRole("button", { name: "Delete the organization…" });
  await opener.click();
  const dialog = admin.getByRole("dialog", { name: "Delete Contoso (synthetic)?" });
  await expect(dialog).toContainText("A final export is made now");
  // What the purge erases, accounts included, and how long the installation's backups still hold the data
  // (GET organization/deletion's backups_kept_days: 56 with the default 14 daily and 8 weekly sets).
  await expect(dialog).toContainText("Everything in it is erased in 30 days");
  await expect(dialog).toContainText("Members' accounts that belong to no other organization are erased too: their email address, name and password are removed.");
  await expect(dialog.getByTestId("deletion-backups")).toHaveText("Backups of this installation taken before then still hold its data for up to 56 days (about 8 weeks) after the erasure, until those backups are pruned.");
  await expectNoViolations(admin, "Delete organization dialog");
  // The slug must be typed exactly; then the current password is checked (403 reauthentication-required).
  await dialog.getByRole("button", { name: "Delete Contoso (synthetic)" }).click();
  await expect(dialog.getByRole("alert")).toContainText(`Type the organization's slug, ${CLOUD_ORG_SLUG}, exactly to confirm.`);
  await dialog.getByLabel(`Type ${CLOUD_ORG_SLUG} to confirm (required)`).fill(CLOUD_ORG_SLUG);
  await dialog.getByLabel("Your current password").fill("not-the-password");
  await dialog.getByRole("button", { name: "Delete Contoso (synthetic)" }).click();
  await expect(dialog.getByLabel("Your current password")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByRole("alert")).toContainText("Enter your current password to confirm.");
  await dialog.getByLabel("Your current password").fill(PASSWORD);
  await dialog.getByRole("button", { name: "Delete Contoso (synthetic)" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(section.getByRole("button", { name: "Cancel the deletion" })).toBeVisible();
  await expect(section.getByTestId("deletion-scheduled")).toContainText("Scheduled for deletion on");
  await expect(section.getByTestId("deletion-scheduled")).toContainText("Queued");
  await expect(section.getByTestId("deletion-scheduled")).toContainText("Backups taken before then hold the data for up to 56 days (about 8 weeks) more.");
  // The banner, with the final export for Admins.
  const banner = admin.getByTestId("deletion-banner");
  await expect(banner).toContainText("Contoso (synthetic) is scheduled for deletion on");
  await expect(banner.getByTestId("final-export")).toContainText("Final export: being prepared.");
  await expectNoViolations(admin, "Organization settings, deletion scheduled");

  // Every member sees it, on every page; only Admins see the final export.
  const viewer = await signedIn(browser, VIEWER);
  const viewerBanner = viewer.getByTestId("deletion-banner");
  await expect(viewerBanner).toContainText("Ask an Admin if this is a mistake.");
  await expect(viewerBanner.getByTestId("final-export")).toHaveCount(0);
  await viewer.goto("/applications");
  await expect(viewerBanner).toBeVisible();
  await expectNoViolations(viewer, "Applications with the deletion banner (Viewer)");

  // An Admin cancels: the banner goes.
  await section.getByRole("button", { name: "Cancel the deletion" }).click();
  await expect(admin.getByRole("status").filter({ hasText: "The deletion is canceled." })).toBeAttached();
  await expect(banner).toHaveCount(0);
  await expect(section.getByRole("button", { name: "Delete the organization…" })).toBeVisible();
  await viewer.reload();
  await expect(viewer.getByRole("heading", { level: 1, name: "Applications" })).toBeVisible();
  await expect(viewerBanner).toHaveCount(0);
  await viewer.context().close();
  await admin.context().close();
});

test("Platform console: staff with no membership land in it, open an organization, override a capability and a limit, impersonate a member and stop", async ({ browser }) => {
  // platform@example.test belongs to no organization: signing in lands in the console, with no organization UI.
  const staff = await signedIn(browser, PLATFORM_EMAIL);
  await expect(staff).toHaveURL(/\/platform\/organizations$/);
  await expect(staff.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
  await expect(staff.getByRole("link", { name: /^Back to / })).toHaveCount(0);
  await expect(staff.getByText("Signed in as Pat Platform")).toBeVisible();
  await staff.goto("/dashboard");
  await expect(staff).toHaveURL(/\/platform\/organizations$/); // no organization page to go to
  const orgs = staff.getByRole("region", { name: "Organizations table" });
  await expect(orgs.getByRole("rowheader", { name: /Contoso \(synthetic\)/ })).toBeVisible();
  await expectNoViolations(staff, "Platform organizations");
  // Filters live in the URL. No organization here is community: Contoso was moved to cloud at start-up, and the
  // organizations the signup tests made are cloud too (signup's edition), so this filter's empty state relies on that.
  await staff.getByLabel("Edition").selectOption("community");
  await expect(staff).toHaveURL(/edition=community/);
  await expect(staff.getByText("No organizations match these filters")).toBeVisible();
  await staff.getByLabel("Edition").selectOption("cloud");
  await expect(staff).toHaveURL(/edition=cloud/);
  await orgs.getByRole("link", { name: "Contoso (synthetic)" }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();
  await expect(staff.getByText("Cloud edition", { exact: false }).first()).toBeVisible();
  await expectNoViolations(staff, "Platform organization page");

  // A capability override, with its reason required.
  await staff.getByRole("button", { name: "Add capability override" }).click();
  const grant = staff.getByRole("dialog", { name: "Add a capability override" });
  await grant.getByRole("button", { name: "Grant the capability" }).click();
  await expect(grant.getByRole("alert")).toContainText("Give a reason");
  await grant.getByLabel("Capability (required)").fill("assistant");
  await grant.getByLabel("Reason (required)").fill("Trial of the assistant agreed with the customer (synthetic).");
  await expectNoViolations(staff, "Capability override dialog");
  await grant.getByRole("button", { name: "Grant the capability" }).click();
  await expect(grant).toHaveCount(0);
  await expect(staff.getByRole("status").filter({ hasText: "assistant is granted to Contoso (synthetic)." })).toBeAttached();
  const overrides = staff.getByRole("region", { name: "Capability overrides table" });
  await expect(overrides.getByRole("row").filter({ hasText: "assistant" })).toContainText("+Granted");
  await expect(overrides.getByRole("row").filter({ hasText: "assistant" })).toContainText("✓Live");

  // A limit override: no more records. The Admin's create is refused with the limit named.
  await staff.getByRole("button", { name: "Set the Catalog records limit" }).click();
  const limit = staff.getByRole("dialog", { name: "Set a limit override" });
  await limit.getByLabel(/^Maximum/).fill("0");
  await limit.getByLabel("Reason (required)").fill("Testing the records limit (synthetic).");
  await expectNoViolations(staff, "Limit override dialog");
  await limit.getByRole("button", { name: "Set Catalog records" }).click();
  await expect(limit).toHaveCount(0);
  await expect(staff.getByRole("region", { name: "Limits table" }).getByRole("row").filter({ hasText: "Catalog records" })).toContainText("Testing the records limit");
  // And the members limit at the current usage (three demo accounts).
  await staff.getByRole("button", { name: "Set the Active members limit" }).click();
  await limit.getByLabel(/^Maximum/).fill("3");
  await limit.getByLabel("Reason (required)").fill("Testing the members limit (synthetic).");
  await limit.getByRole("button", { name: "Set Active members" }).click();
  await expect(limit).toHaveCount(0);

  const admin = await signedIn(browser, ADMIN);
  await admin.goto("/applications/new");
  await admin.getByLabel("Name (required)").fill(`Limited ${Date.now()}`);
  await admin.getByRole("button", { name: "Save" }).click();
  await expect(admin.getByRole("alert").filter({ hasText: "Limit reached: Catalog records, 0 of 0 used." })).toBeVisible();
  await expectNoViolations(admin, "Record create refused by a limit");
  await admin.goto("/admin/users");
  await admin.getByRole("button", { name: "Invite user" }).click();
  const inviteFull = admin.getByRole("dialog", { name: "Invite user" });
  await inviteFull.getByLabel("Email (required)").fill(`one.too.many.${Date.now()}@example.test`);
  await inviteFull.getByRole("button", { name: "Invite" }).click();
  await expect(inviteFull.getByRole("alert")).toContainText("Limit reached: Active members, 3 of 3 used.");
  await inviteFull.getByRole("button", { name: "Cancel" }).click();
  await admin.goto("/admin/limits");
  await expect(admin.getByTestId("limit-records")).toContainText("!Limit reached");
  await expect(admin.getByTestId("limit-records")).toContainText("Set for this organization by the service operator");
  await expect(admin.getByTestId("limit-members")).toContainText("3 of 3 members used");

  const remove = staff.getByRole("dialog", { name: "Remove the limit override?" });
  for (const name of ["Catalog records", "Active members"]) {
    await staff.getByRole("button", { name: `Remove override of ${name}` }).click();
    await remove.getByLabel("Reason (required)").fill("Test finished (synthetic).");
    await remove.getByRole("button", { name: "Remove the override" }).click();
    await expect(staff.getByRole("status").filter({ hasText: `The ${name} override is removed` })).toBeAttached();
  }

  // Suspension: every member gets the suspended page instead of any screen, until it is lifted.
  await staff.getByRole("button", { name: "Suspend" }).click();
  const suspend = staff.getByRole("dialog", { name: "Suspend Contoso (synthetic)?" });
  await suspend.getByLabel("Reason (required)").fill("Testing suspension (synthetic).");
  await expectNoViolations(staff, "Suspend dialog");
  await suspend.getByRole("button", { name: "Suspend Contoso (synthetic)" }).click();
  await expect(staff.getByText(/Suspended on .*: Testing suspension \(synthetic\)\./)).toBeVisible();
  await admin.goto("/applications");
  await expect(admin.getByRole("heading", { level: 1, name: "Contoso (synthetic) is suspended" })).toBeVisible();
  await expect(admin.getByTestId("suspended")).toContainText("Nothing has been removed");
  await expectNoViolations(admin, "Suspended organization");
  await staff.getByRole("button", { name: "Unsuspend" }).click();
  const unsuspend = staff.getByRole("dialog", { name: "Unsuspend Contoso (synthetic)?" });
  await unsuspend.getByLabel("Reason (required)").fill("Suspension test over (synthetic).");
  await expectNoViolations(staff, "Unsuspend dialog");
  await unsuspend.getByRole("button", { name: "Unsuspend Contoso (synthetic)" }).click();
  await expect(staff.getByRole("status").filter({ hasText: "Contoso (synthetic) is no longer suspended." })).toBeAttached();
  await admin.reload();
  await expect(admin.getByRole("heading", { level: 1, name: "Applications" })).toBeVisible();
  await admin.context().close();

  // Impersonate the Admin: a reason and the staff password, then the member's dashboard under a permanent banner.
  const members = staff.getByRole("region", { name: "Members table" });
  await expect(members.getByRole("row").filter({ hasText: STAFF_EMAIL })).toContainText("Staff can't be impersonated");
  await members.getByRole("button", { name: "Impersonate Alex Admin" }).click();
  const imp = staff.getByRole("dialog", { name: "Impersonate Alex Admin" });
  await imp.getByLabel("Reason (required)").fill("Customer asked for help with their invitations (synthetic).");
  await imp.getByLabel("Your current password").fill("not-the-password");
  await imp.getByRole("button", { name: "Impersonate Alex Admin" }).click();
  await expect(imp.getByLabel("Your current password")).toHaveAttribute("aria-invalid", "true");
  await expectNoViolations(staff, "Impersonate dialog");
  await imp.getByLabel("Your current password").fill(PASSWORD);
  await imp.getByRole("button", { name: "Impersonate Alex Admin" }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  const banner = staff.getByTestId("impersonation-banner");
  await expect(banner).toContainText("You are impersonating Alex Admin (admin@example.test) in Contoso (synthetic)");
  await expect(banner).toContainText("As platform staff Pat Platform. Reason: Customer asked for help with their invitations (synthetic).");
  await expect(banner).toContainText(/Ends in (29|30):\d{2}/);
  await expectNoViolations(staff, "Dashboard while impersonating");

  // A restricted action is refused with a plain message; the banner stays on every page.
  await staff.goto("/admin/users");
  await expect(banner).toBeVisible();
  await staff.getByRole("button", { name: "Invite user" }).click();
  const invite = staff.getByRole("dialog", { name: "Invite user" });
  await invite.getByLabel("Email (required)").fill(`impersonated.${Date.now()}@example.test`);
  await invite.getByRole("button", { name: "Invite" }).click();
  await expect(invite.getByRole("alert")).toContainText(/impersonating/);
  await invite.getByRole("button", { name: "Cancel" }).click();
  // The console itself is closed while impersonating.
  await staff.goto("/platform/organizations");
  await expect(staff.getByRole("heading", { level: 1, name: "Stop impersonating to use the console" })).toBeVisible();
  await expectNoViolations(staff, "Platform console while impersonating");

  // Stop: back to the organization in the console, as staff again.
  await banner.getByRole("button", { name: "Stop impersonating" }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();
  await expect(banner).toHaveCount(0);
  await expect(staff.getByRole("status").filter({ hasText: "You stopped impersonating Alex Admin." })).toBeAttached();
  const organizationPage = staff.url();
  const events = staff.getByRole("region", { name: /The newest platform events about/ });
  await expect(events.getByRole("row").filter({ hasText: "impersonation.started" })).toBeVisible();
  await expect(events.getByRole("row").filter({ hasText: "impersonation.stopped" })).toBeVisible();

  // The platform audit stream, filtered by action in the URL.
  await staff.getByRole("link", { name: "Audit events" }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Platform audit events" })).toBeVisible();
  await staff.getByLabel("Action").selectOption("capability.granted");
  await expect(staff).toHaveURL(/action=capability\.granted/);
  await expect(staff.getByRole("region", { name: "Platform audit events, newest first" }).getByRole("row").filter({ hasText: "assistant" })).toBeVisible();
  await expectNoViolations(staff, "Platform audit events");

  // Tidy up: the capability override goes, so the installation is as it started.
  await staff.goto(organizationPage);
  await expect(staff.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();
  await staff.getByRole("button", { name: "Remove the assistant override" }).click();
  const removeCap = staff.getByRole("dialog", { name: "Remove the capability override?" });
  await removeCap.getByLabel("Reason (required)").fill("Trial over (synthetic).");
  await removeCap.getByRole("button", { name: "Remove the assistant override" }).click();
  await expect(staff.getByRole("status").filter({ hasText: "The assistant override is removed." })).toBeAttached();
  await staff.context().close();
});

test("Platform pages that don't exist say so", async ({ browser }) => {
  const staff = await signedIn(browser, PLATFORM_EMAIL);
  await staff.goto("/platform/nothing-here");
  await expect(staff.getByRole("heading", { level: 1, name: "Page not found" })).toBeVisible();
  await expectNoViolations(staff, "Platform page not found");
  await staff.goto("/platform/organizations/00000000-0000-4000-8000-000000000000");
  await expect(staff.getByRole("heading", { level: 1, name: "Organization not found" })).toBeVisible();
  await expect(staff.getByRole("alert")).toContainText("No organization has this ID.");
  await expectNoViolations(staff, "Platform organization not found");
  await staff.context().close();
});

test("System status: a cloud organization's Admin gets its own part, and staff read the installation in the console", async ({ browser }) => {
  // Contoso is on the cloud edition (billing): its Admin gets the organization view, whatever its place on the installation.
  const admin = await signedIn(browser, ADMIN);
  await admin.goto("/admin/system");
  await expect(admin.getByRole("heading", { level: 1, name: "System status" })).toBeVisible();
  await expect(admin.getByTestId("organization-view")).toBeVisible();
  await expect(admin.getByRole("region", { name: "Worker" })).toBeVisible();
  await expect(admin.getByRole("region", { name: /^Schedules(?! table)/ })).toHaveCount(0);
  await expect(admin.getByRole("region", { name: "Backups" })).toHaveCount(0);
  await expectNoViolations(admin, "System status, organization view (cloud)");
  await admin.context().close();

  // Staff with no membership: the console's System page, from its navigation, is the installation view.
  const staff = await signedIn(browser, PLATFORM_EMAIL);
  await staff.getByRole("navigation", { name: "Platform" }).getByRole("link", { name: "System" }).click();
  await expect(staff).toHaveURL(/\/platform\/system$/);
  await expect(staff.getByRole("heading", { level: 1, name: "System status" })).toBeVisible();
  await expect(staff.getByRole("region", { name: /^Schedules(?! table)/ })).toBeVisible();
  await expect(staff.getByRole("region", { name: /^Schedules(?! table)/ })).toContainText("org.purge_deleted");
  await expect(staff.getByRole("region", { name: "Backups" })).toBeVisible();
  await expect(staff.getByTestId("organization-view")).toHaveCount(0);
  await expect(staff.getByRole("region", { name: "Job counts table" }).getByRole("columnheader")).toHaveText(["Status", "Installation-wide"]);
  await expectNoViolations(staff, "Platform console System status");

  // Installation-wide warnings, stored secrets among them, are listed with the operator's fix; the backup key's
  // acknowledgement goes through the platform route.
  await staff.route((url) => url.pathname === "/api/v1/platform/system", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const body = await response.json() as { backup: Record<string, unknown> };
    await route.fulfill({
      response,
      json: {
        ...body, backup: { ...body.backup, key_source: "generated", key_present: true, key_acknowledged: false, key_acknowledged_at: null },
        warnings: [
          { code: "backup_key_unacknowledged", message: "Backup key is stored on this host: copy it somewhere safe." },
          { code: "stored_secrets_unreadable", message: "Some stored secrets were encrypted under a credential key this installation doesn't have, so they can't be read." },
        ],
      },
    });
  });
  let acknowledgedThrough = "";
  await staff.route((url) => url.pathname.endsWith("/backup/acknowledge-key"), async (route) => {
    acknowledgedThrough = new URL(route.request().url()).pathname;
    await route.fulfill({ status: 409, contentType: "application/problem+json", json: { type: "/problems/conflict", title: "Conflict", status: 409, detail: "There is no backup key yet." } });
  });
  await staff.reload();
  const secrets = staff.getByRole("region", { name: "To fix" }).locator('li[data-code="stored_secrets_unreadable"]');
  await expect(secrets).toContainText("SOFTWARDEN_CREDENTIAL_KEYS");
  await staff.getByRole("button", { name: "I have copied the backup key somewhere safe…" }).click();
  const dialog = staff.getByRole("dialog", { name: "Confirm the backup key is copied" });
  await dialog.getByRole("button", { name: "Confirm the key is copied" }).click();
  await expect(dialog.getByRole("alert")).toContainText("There is no backup key yet.");
  expect(acknowledgedThrough).toBe("/api/v1/platform/system/backup/acknowledge-key");
  await expectNoViolations(staff, "Platform console System status with warnings");
  await staff.context().close();
});

test("Sign-in domains: a claim is pending with its TXT record until verified; staff look a domain up, release it and move it", async ({ browser }) => {
  // The cloud server asks for proof (SOFTWARDEN_SSO_DOMAIN_VERIFICATION); its simulated resolver verifies any
  // *.dns-verified.test domain and no other.
  const stamp = Date.now();
  const proved = `contoso-${stamp}.dns-verified.test`;
  const unproved = `contoso-${stamp}.example`;
  const admin = await signedIn(browser, ADMIN);
  await admin.goto("/admin/sign-in");
  await expect(admin.getByRole("heading", { level: 1, name: "Single sign-on" })).toBeVisible();
  await admin.getByLabel("Domains", { exact: true }).fill(`${proved}, ${unproved}`);
  await admin.getByRole("button", { name: "Save domains" }).click();
  await expect(admin.getByTestId("sso-status")).toContainText("are pending: publish the TXT record shown below, then verify.");
  const states = admin.getByRole("list", { name: "Sign-in domain states" });
  const good = states.locator(`li[data-domain="${proved}"]`);
  const bad = states.locator(`li[data-domain="${unproved}"]`);
  await expect(good).toContainText("◐Pending verification");
  await expect(good).toContainText("Finds nobody on the sign-in page until it is verified; forgotten on");
  await expect(good).toContainText(`_softwarden-challenge.${proved}`);
  await expect(good).toContainText("softwarden-domain-verification=");
  await expectNoViolations(admin, "Single sign-on, sign-in domains pending");

  // Not proved: the API's one neutral refusal, beside the domain.
  await bad.getByRole("button", { name: `Verify ${unproved}` }).click();
  await expect(bad.getByRole("alert")).toContainText(`We couldn't confirm ${unproved} for this organization.`);
  await expect(bad).toContainText("◐Pending verification");
  await expectNoViolations(admin, "Single sign-on, a domain not verified");
  // Proved: verified, and focus moves to its new state since the Verify button is gone.
  await good.getByRole("button", { name: `Verify ${proved}` }).click();
  await expect(good).toContainText("✓Verified");
  await expect(good).toContainText("Finds this organization on the sign-in page");
  await expect(good.locator("[tabindex='-1']")).toBeFocused();
  await expect(admin.getByTestId("sso-status")).toContainText(`${proved} is verified`);

  // Staff: look the domain up, release it, then move the other one to its claimant.
  const staff = await signedIn(browser, PLATFORM_EMAIL);
  await staff.getByRole("navigation", { name: "Platform" }).getByRole("link", { name: "Sign-in domains" }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Sign-in domains" })).toBeVisible();
  await expect(staff.getByText("Look up a domain")).toBeVisible();
  await staff.getByLabel("Domain", { exact: true }).fill(proved);
  await staff.getByRole("button", { name: "Look up" }).click();
  await expect(staff).toHaveURL(new RegExp(`domain=${proved.replace(/\./g, "\\.")}`));
  await expect(staff.getByTestId("domain-holder")).toContainText(`Contoso (synthetic) holds it: addresses at ${proved} find that organization`);
  const claims = staff.getByRole("region", { name: "Sign-in domain claims table" });
  await expect(claims.getByRole("row", { name: /Contoso \(synthetic\)/ })).toContainText("✓Verified, holds it");
  await expectNoViolations(staff, "Platform sign-in domains, a verified holder");
  await staff.getByRole("button", { name: `Release ${proved}…` }).click();
  const release = staff.getByRole("dialog", { name: `Release ${proved}?` });
  await release.getByRole("button", { name: `Release ${proved}` }).click();
  await expect(release.getByRole("alert")).toContainText("Give a reason");
  await release.getByLabel("Reason (required)").fill("The customer asked to free it (synthetic).");
  await expectNoViolations(staff, "Release a sign-in domain dialog");
  await release.getByRole("button", { name: `Release ${proved}` }).click();
  await expect(staff.getByRole("status").filter({ hasText: `${proved} is released from Contoso (synthetic).` })).toBeAttached();
  await expect(staff.getByText(`No organization claims ${proved}`)).toBeVisible();
  // Moved to an organization that has no claim of it, chosen with the picker (review follow-up).
  await staff.getByRole("button", { name: `Move ${proved} to another organization…` }).click();
  const anyOrg = staff.getByRole("dialog", { name: `Move ${proved} to another organization?` });
  await anyOrg.getByRole("button", { name: `Move ${proved}`, exact: true }).click();
  await expect(anyOrg.getByRole("alert")).toContainText("Choose the organization that gets the domain.");
  await anyOrg.getByLabel("Find the organization").fill("contoso");
  await anyOrg.getByLabel("Organization (required)").selectOption({ label: `Contoso (synthetic) (${CLOUD_ORG_SLUG})` });
  await anyOrg.getByLabel("Reason (required)").fill("The customer proved the domain to support (synthetic).");
  await expectNoViolations(staff, "Move a sign-in domain to another organization");
  await anyOrg.getByRole("button", { name: `Move ${proved} to Contoso (synthetic)` }).click();
  await expect(staff.getByRole("status").filter({ hasText: `${proved} is now verified for Contoso (synthetic).` })).toBeAttached();
  await expect(claims.getByRole("row", { name: /Contoso \(synthetic\)/ })).toContainText("✓Verified, holds it");

  await staff.getByLabel("Domain", { exact: true }).fill(unproved);
  await staff.getByRole("button", { name: "Look up" }).click();
  await expect(staff.getByTestId("domain-holder")).toContainText("Nobody holds it verified");
  await expect(claims.getByRole("row", { name: /Contoso \(synthetic\)/ })).toContainText("◐Pending");
  await claims.getByRole("button", { name: "Move to this organization… Contoso (synthetic)" }).click();
  const move = staff.getByRole("dialog", { name: `Move ${unproved} to Contoso (synthetic)?` });
  await move.getByLabel("Reason (required)").fill("Checked with the customer that they control the domain (synthetic).");
  await expectNoViolations(staff, "Move a sign-in domain dialog");
  await move.getByRole("button", { name: `Move ${unproved} to Contoso (synthetic)` }).click();
  await expect(staff.getByRole("status").filter({ hasText: `${unproved} is now verified for Contoso (synthetic).` })).toBeAttached();
  await expect(claims.getByRole("row", { name: /Contoso \(synthetic\)/ })).toContainText("✓Verified, holds it");
  await staff.getByRole("link", { name: "Audit events" }).click();
  await staff.getByLabel("Action").selectOption("sign_in_domain.moved");
  await expect(staff.getByRole("region", { name: "Platform audit events, newest first" }).getByRole("row").filter({ hasText: unproved })).toBeVisible();
  await staff.context().close();

  // Tidy up: Contoso keeps no sign-in domain.
  await admin.reload();
  await admin.getByLabel("Domains", { exact: true }).fill("");
  await admin.getByRole("button", { name: "Save domains" }).click();
  await expect(admin.getByTestId("sso-status")).toContainText("Sign-in domains removed.");
  await admin.context().close();
});

test("Suspending an organization with a scheduled deletion: the dialog says it is paused, the state shows it, and unsuspending resumes it", async ({ browser }) => {
  const admin = await signedIn(browser, ADMIN);
  await post(admin, "organization/deletion", { confirm: CLOUD_ORG_SLUG, password: PASSWORD });
  const staff = await signedIn(browser, PLATFORM_EMAIL);
  const { orgId } = await contosoMember(staff, ADMIN);
  try {
    await staff.goto(`/platform/organizations/${orgId}`);
    await expect(staff.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();
    await expect(staff.getByTestId("platform-deletion")).toContainText("Scheduled for deletion on");
    const opener = staff.getByRole("button", { name: "Suspend" });
    await opener.click();
    const suspend = staff.getByRole("dialog", { name: "Suspend Contoso (synthetic)?" });
    await expect(suspend.getByTestId("suspend-deletion")).toContainText(/Contoso \(synthetic\) is scheduled for deletion on .*, (29|30) days from now\./);
    await expect(suspend.getByTestId("suspend-deletion")).toContainText("Suspending pauses it: nothing is purged while it is suspended, and its Admins can't cancel it.");
    await expectNoViolations(staff, "Suspend dialog with a scheduled deletion");
    await suspend.getByLabel("Reason (required)").fill("Billing dispute under review (synthetic).");
    await suspend.getByRole("button", { name: "Suspend Contoso (synthetic)" }).click();
    await expect(staff.getByRole("status").filter({ hasText: "Contoso (synthetic) is suspended. Its members are refused until you unsuspend it. Its scheduled deletion is paused." })).toBeAttached();
    await expect(staff.getByText(/Deletion paused \(was due /)).toBeVisible();
    await expect(staff.getByTestId("platform-deletion")).toContainText("Paused while it is suspended: nothing is purged");
    await expectNoViolations(staff, "Organization suspended with its deletion paused");

    await staff.getByRole("button", { name: "Unsuspend" }).click();
    const unsuspend = staff.getByRole("dialog", { name: "Unsuspend Contoso (synthetic)?" });
    await expect(unsuspend.getByTestId("unsuspend-deletion")).toContainText("Its scheduled deletion resumes");
    await unsuspend.getByLabel("Reason (required)").fill("Dispute settled (synthetic).");
    await unsuspend.getByRole("button", { name: "Unsuspend Contoso (synthetic)" }).click();
    await expect(staff.getByRole("status").filter({ hasText: /Contoso \(synthetic\) is no longer suspended\. Its deletion resumes: now due on / })).toBeAttached();
    await expect(staff.getByText(/Deletion paused/)).toHaveCount(0);
  } finally {
    // Leave Contoso as it was: unsuspended (if a step above failed) and with no deletion scheduled.
    const detail: { suspended_at: string | null } = await (await staff.request.get(`/api/v1/platform/organizations/${orgId}`)).json();
    if (detail.suspended_at) await post(staff, `platform/organizations/${orgId}/unsuspend`, { reason: "Tidy up (synthetic)." });
    await post(admin, "organization/deletion", undefined, "DELETE");
    await staff.context().close();
    await admin.context().close();
  }
});

test("A deletion whose purge keeps failing says so in the console", async ({ browser }) => {
  const staff = await signedIn(browser, PLATFORM_EMAIL);
  const { orgId } = await contosoMember(staff, ADMIN);
  // The purge failing is the worker's doing; the console's answer is shaped as the API sends it for such an organization.
  const attempted = new Date(Date.now() - 15 * 60_000).toISOString();
  await staff.route((url) => url.pathname === `/api/v1/platform/organizations/${orgId}`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, deletion: { requested_at: attempted, due_at: attempted, requested_by: null, paused: false, purge_attempted_at: attempted, purge_failures: 3 } } });
  });
  await staff.goto(`/platform/organizations/${orgId}`);
  const failures = staff.getByTestId("purge-failures");
  await expect(failures).toContainText("The purge failed 3 times in a row, last tried");
  await expect(failures).toContainText("shows as failing on the System page");
  await expectNoViolations(staff, "Organization whose purge keeps failing");
  await failures.getByRole("link", { name: "System page" }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "System status" })).toBeVisible();
  await staff.context().close();
});

test("Impersonation banner: a browser clock past the end re-checks and stays while the server says it is live, then returns to the console once it has ended", async ({ browser }) => {
  const context = await browser.newContext({ baseURL: CLOUD_WEB });
  const staff = await context.newPage();
  await staff.clock.install();
  await staff.goto("/login");
  await staff.getByLabel("Email").fill(PLATFORM_EMAIL);
  await staff.getByLabel("Password").fill(PASSWORD);
  await staff.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Organizations" })).toBeVisible();
  // Start impersonating the Admin through the API, as the console's dialog does.
  const { orgId, userId: adminId } = await contosoMember(staff, ADMIN);
  await post(staff, "platform/impersonation", { organization_id: orgId, user_id: adminId, reason: "Clock skew test (synthetic).", password: PASSWORD });
  await staff.goto("/dashboard");
  const banner = staff.getByTestId("impersonation-banner");
  await expect(banner).toContainText(/Ends in (29|30):\d{2}/);

  // A check that gets no clear answer (a 502 while the server is being deployed) keeps the impersonation on screen:
  // it neither signs out nor returns to the console.
  await staff.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 502, contentType: "application/problem+json", body: JSON.stringify({ type: "about:blank", title: "Bad Gateway", status: 502, detail: "Bad gateway." }) }), { times: 1 });
  const failed = staff.waitForResponse((r) => r.url().endsWith("/api/v1/auth/me") && r.status() === 502);
  await staff.evaluate(() => window.dispatchEvent(new Event("focus")));
  await failed;
  await staff.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))); // handled
  await expect(staff).toHaveURL(/\/dashboard$/);
  await expect(banner).toContainText("You are impersonating Alex Admin");
  await expect(staff.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();

  // A 403 not-signed-in to a request that carried a session key rotated a moment before (another tab's Start or Stop)
  // is read once more after a fresh CSRF read (docs/API.md "Session key changes"): the tab stays, signed in.
  await staff.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 403, contentType: "application/problem+json", body: JSON.stringify({ type: "/problems/not-signed-in", title: "Not signed in", status: 403, detail: "Authentication credentials were not provided." }) }), { times: 1 });
  const notSignedIn = staff.waitForResponse((r) => r.url().endsWith("/api/v1/auth/me") && r.status() === 403);
  const retried = staff.waitForResponse((r) => r.url().endsWith("/api/v1/auth/me") && r.status() === 200);
  await staff.evaluate(() => window.dispatchEvent(new Event("focus")));
  await notSignedIn;
  await retried;
  await expect(staff).toHaveURL(/\/dashboard$/);
  await expect(banner).toContainText("You are impersonating Alex Admin");

  // The browser's clock jumps 31 minutes ahead; the server's hasn't moved. At 0:00 the banner re-checks `auth/me`,
  // finds the impersonation live, corrects for the server's clock and keeps counting.
  await staff.clock.fastForward("31:00");
  await expect(banner).toContainText(/Ends in (29|30):\d{2}/, { timeout: 10_000 });
  await expect(staff.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();

  // It ends on the server without this page being told (here: stopped through the API, as a request past its end
  // would); the next re-check (every 20 seconds) returns to the console.
  await post(staff, "platform/impersonation", undefined, "DELETE");
  await staff.clock.runFor(21_000);
  await expect(staff.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();
  await expect(banner).toHaveCount(0);
  await expect(staff.getByRole("status").filter({ hasText: "Your impersonation ended." })).toBeAttached();
  await context.close();
});

test("Impersonation ending on the server: the next write isn't made, and the tab returns to the console", async ({ browser }) => {
  const staff = await signedIn(browser, PLATFORM_EMAIL);
  const { orgId, userId } = await contosoMember(staff, ADMIN);
  await post(staff, "platform/impersonation", { organization_id: orgId, user_id: userId, reason: "Expiry test (synthetic).", password: PASSWORD });
  await staff.goto("/applications/new");
  await expect(staff.getByTestId("impersonation-banner")).toBeVisible();
  const name = `Written after expiry ${Date.now()}`;
  await staff.getByLabel("Name (required)").fill(name);
  // Its time is up on the server (the test-only route); this tab doesn't know yet.
  await post(staff, "e2e/expire-impersonation", {});
  const refused = staff.waitForResponse((r) => r.url().endsWith("/api/v1/applications") && r.request().method() === "POST");
  await staff.getByRole("button", { name: "Save" }).click();
  expect((await refused).status()).toBe(409);
  await expect(staff.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();
  await expect(staff.getByTestId("impersonation-banner")).toHaveCount(0);
  await expect(staff.getByRole("status").filter({ hasText: "Your impersonation ended before that change, so it wasn't made." })).toBeAttached();
  await expectNoViolations(staff, "Console after an impersonation ended");
  const admin = await signedIn(browser, ADMIN);
  expect(await applicationNamed(admin, name)).toBe(false);
  await admin.context().close();
  await staff.context().close();
});

test("Two tabs: Stop in one, then a save in the other is refused by its impersonation id and nothing is written", async ({ browser }) => {
  // Staff with a membership (editor@example.test, a Contoso member): the console from the account menu, and the save
  // that must not land in their own organization.
  const tabA = await signedIn(browser, STAFF_EMAIL);
  await openAccountMenu(tabA, "Platform console");
  await expect(tabA.getByRole("heading", { level: 1, name: "Organizations" })).toBeVisible();
  await expect(tabA.getByRole("link", { name: "Back to Contoso (synthetic)" })).toBeVisible();
  const { orgId, userId } = await contosoMember(tabA, ADMIN);
  await post(tabA, "platform/impersonation", { organization_id: orgId, user_id: userId, reason: "Two tab test (synthetic).", password: PASSWORD });
  // Tab A hears nothing from tab B (as in a browser without BroadcastChannel, or a message missed): only its
  // impersonation id, sent with the save, tells the server what it is showing.
  await tabA.addInitScript(() => { Reflect.deleteProperty(window, "BroadcastChannel"); });
  await tabA.goto("/applications/new");
  await expect(tabA.getByTestId("impersonation-banner")).toContainText("You are impersonating Alex Admin");
  const name = `Written from a stale tab ${Date.now()}`;
  await tabA.getByLabel("Name (required)").fill(name);

  const tabB = await tabA.context().newPage();
  await tabB.goto("/dashboard");
  await tabB.getByTestId("impersonation-banner").getByRole("button", { name: "Stop impersonating" }).click();
  await expect(tabB.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();

  const refused = tabA.waitForResponse((r) => r.url().endsWith("/api/v1/applications") && r.request().method() === "POST");
  await tabA.getByRole("button", { name: "Save" }).click();
  const answer = await refused;
  expect(answer.status()).toBe(409);
  expect((await answer.json()).type).toBe("/problems/impersonation-ended");
  await expect(tabA.getByRole("heading", { level: 1, name: "Contoso (synthetic)" })).toBeVisible();
  await expect(tabA.getByRole("status").filter({ hasText: "Your impersonation ended before that change, so it wasn't made." })).toBeAttached();
  await expectNoViolations(tabA, "Console after a stale tab's save was refused");
  // Nothing landed, as the member or as the staff account in its own organization (Contoso for both here).
  expect(await applicationNamed(tabA, name)).toBe(false);
  await tabA.context().close();
});

test("Impersonation started in another tab, unheard: a save from the tab that didn't know is refused (409 started), and that tab shows the impersonation with a notice", async ({ browser }) => {
  // Staff with a membership, in their own Contoso with a form filled. This tab hears nothing from the others (as in a
  // browser without BroadcastChannel, or a message missed).
  const tabA = await signedIn(browser, STAFF_EMAIL);
  await tabA.addInitScript(() => { Reflect.deleteProperty(window, "BroadcastChannel"); });
  await tabA.goto("/applications/new");
  const name = `Written before a start ${Date.now()}`;
  await tabA.getByLabel("Name (required)").fill(name);
  // Another tab of the same browser session starts impersonating the Admin.
  const tabB = await tabA.context().newPage();
  const { orgId, userId } = await contosoMember(tabB, ADMIN);
  await post(tabB, "platform/impersonation", { organization_id: orgId, user_id: userId, reason: "Unheard start test (synthetic).", password: PASSWORD });

  const refused = tabA.waitForResponse((r) => r.url().endsWith("/api/v1/applications") && r.request().method() === "POST");
  await tabA.getByRole("button", { name: "Save" }).click();
  const answer = await refused;
  expect(answer.status()).toBe(409);
  expect((await answer.json()).type).toBe("/problems/impersonation-started");
  await expect(tabA).toHaveURL(/\/dashboard$/);
  await expect(tabA.getByTestId("impersonation-banner")).toContainText("You are impersonating Alex Admin");
  const notice = tabA.getByTestId("session-notice");
  await expect(notice).toContainText("Platform staff started impersonating Alex Admin in another tab of this browser");
  await expect(notice).toContainText("Your change wasn't made.");
  await expect(tabA.getByTestId("shell-announcer")).toContainText("Platform staff started impersonating Alex Admin");
  await expectNoViolations(tabA, "Session notice after a refused save (impersonation started)");
  // Nothing landed, as the staff account or as the member.
  expect(await applicationNamed(tabA, name)).toBe(false);
  await notice.getByRole("button", { name: "Dismiss this notice" }).click();
  await expect(notice).toHaveCount(0);
  await expect(tabA.getByRole("heading", { level: 1, name: "Dashboard" })).toBeFocused();
  await post(tabA, "platform/impersonation", undefined, "DELETE");
  await tabA.context().close();
});

test("Impersonation started in another tab, heard: the tab with a form open moves home with the banner and a notice, and the form is gone", async ({ browser }) => {
  const tabA = await signedIn(browser, STAFF_EMAIL);
  await tabA.goto("/applications/new");
  await tabA.getByLabel("Name (required)").fill(`Never saved ${Date.now()}`);
  // Another tab starts impersonating from the console, which tells the other tabs (BroadcastChannel).
  const tabB = await tabA.context().newPage();
  const { orgId } = await contosoMember(tabB, ADMIN);
  await tabB.goto(`/platform/organizations/${orgId}`);
  await tabB.getByRole("region", { name: "Members table" }).getByRole("button", { name: "Impersonate Alex Admin" }).click();
  const imp = tabB.getByRole("dialog", { name: "Impersonate Alex Admin" });
  await imp.getByLabel("Reason (required)").fill("Heard start test (synthetic).");
  await imp.getByLabel("Your current password").fill(PASSWORD);
  await imp.getByRole("button", { name: "Impersonate Alex Admin" }).click();
  await expect(tabB.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();

  // Tab A hears it, reloads its session and moves with it: the form for the staff account's own organization goes.
  await expect(tabA).toHaveURL(/\/dashboard$/);
  await expect(tabA.getByLabel("Name (required)")).toHaveCount(0);
  await expect(tabA.getByTestId("impersonation-banner")).toContainText("You are impersonating Alex Admin");
  const notice = tabA.getByTestId("session-notice");
  await expect(notice).toContainText("Platform staff started impersonating Alex Admin in another tab of this browser, so this tab now shows that, with the banner.");
  await expect(notice).not.toContainText("wasn't made"); // nothing was refused
  await expectNoViolations(tabA, "Session notice after another tab started an impersonation");
  await notice.getByRole("button", { name: "Dismiss this notice" }).click();
  await expect(tabA.getByRole("heading", { level: 1, name: "Dashboard" })).toBeFocused();
  await post(tabA, "platform/impersonation", undefined, "DELETE");
  await tabA.context().close();
});

test("Sign-out refused as not signed in (a session key rotated a moment before) is sent once more, and the browser is signed out", async ({ browser }) => {
  const viewer = await signedIn(browser, VIEWER);
  // The first sign-out is answered as the server answers one that carried a just-rotated key; the second goes through.
  let calls = 0;
  await viewer.route("**/api/v1/auth/logout", async (route) => {
    calls++;
    if (calls === 1) {
      await route.fulfill({ status: 403, contentType: "application/problem+json", body: JSON.stringify({ type: "/problems/not-signed-in", title: "Not signed in", status: 403, detail: "Authentication credentials were not provided." }) });
    } else {
      await route.continue();
    }
  });
  await openAccountMenu(viewer, "Sign out");
  await expect(viewer.getByRole("heading", { level: 1, name: "Sign in to Softwarden" })).toBeVisible();
  expect(calls).toBe(2);
  // Really signed out on the server: a signed-in page sends the browser to sign in again.
  await viewer.goto("/dashboard");
  await expect(viewer).toHaveURL(/\/login$/);
  await expect(viewer.getByRole("heading", { level: 1, name: "Sign in to Softwarden" })).toBeVisible();
  await viewer.context().close();
});
