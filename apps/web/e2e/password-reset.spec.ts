import { expect, test, type Browser, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { emailedInvitation, PASSWORD, pageAs, post } from "./helpers";

/** Self-service password reset. The sign-in page links to /forgot-password, which answers
 *  the same for any address; the emailed link (/reset-password#<token>, read from the e2e outbox after the worker's job
 *  runs) sets a new password and every older session of the account ends; a used link can't be used again. While
 *  platform staff impersonate a member, the page shows the API's refusal. axe in light and dark at each step. */

const FIRST = "synthetic-reset-first-2026";
const SECOND = "synthetic-reset-second-2026";

/** A new password account in Contoso: invited by its Admin, accepted through the emailed link. */
async function newAccount(browser: Browser, baseURL: string | undefined, admin: Page, email: string) {
  await post(admin, "invitations", { email, role: "viewer", name: "Reset Person" });
  const token = (await emailedInvitation(admin, email)).split("/invite#")[1];
  const context = await browser.newContext({ baseURL });
  await context.request.get("/api/v1/auth/csrf");
  const csrf = (await context.cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const res = await context.request.post("/api/v1/auth/invitations/accept", { data: { token, name: "Reset Person", password: FIRST }, headers: { "X-CSRFToken": csrf } });
  expect(res.ok(), await res.text()).toBeTruthy();
  return context; // signed in as the new account
}

async function askForLink(page: Page, email: string) {
  await page.getByLabel("Email (required)").fill(email);
  await page.getByRole("button", { name: "Email me a link" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Check your email" })).toBeVisible();
  return (await page.getByTestId("reset-sent").textContent()) ?? "";
}

test("Forgot your password: the same answer for any address, the emailed link sets a new one, older sessions end and the link works once", async ({ browser, baseURL }) => {
  const stamp = Date.now();
  const email = `reset.${stamp}@example.test`;
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const old = await newAccount(browser, baseURL, admin, email);
  const oldPage = await old.newPage();
  await oldPage.goto("/renewals");
  await expect(oldPage.getByRole("heading", { level: 1, name: "Renewals" })).toBeVisible();

  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto("/login");
  await page.getByRole("link", { name: "Forgot your password?" }).click();
  await expect(page).toHaveURL(/\/forgot-password$/);
  await expect(page.getByRole("heading", { level: 1, name: "Reset your password" })).toBeVisible();
  await expectNoViolations(page, "Forgot your password");
  await page.getByRole("button", { name: "Email me a link" }).click();
  await expect(page.getByLabel("Email (required)")).toHaveAttribute("aria-invalid", "true");

  // An address with no account gets exactly the same words as one with an account.
  const unknown = await askForLink(page, `nobody.${stamp}@example.test`);
  await expectNoViolations(page, "Check your email (reset)");
  await page.getByRole("button", { name: "Use a different email" }).click();
  const known = await askForLink(page, email);
  expect(known).toBe(unknown);

  // The worker sends the link (its job runs here); it is in the outbox for that address only.
  await post(admin, "e2e/run-password-reset-jobs", {});
  const mail: { body: string }[] = await post(admin, "e2e/outbox", { to: email, clear: true });
  const link = mail.map((m) => m.body.match(/\/reset-password#[\w-]+/)?.[0]).find((l) => !!l);
  expect(link, "a reset link emailed").toBeTruthy();

  // The token leaves the address bar as soon as the page reads it.
  await page.goto(link!);
  await expect(page.getByRole("heading", { level: 1, name: "Choose a new password" })).toBeVisible();
  await expect(page).toHaveURL(/\/reset-password$/);
  await expectNoViolations(page, "Choose a new password");
  await page.getByLabel("New password (required)", { exact: true }).fill(SECOND);
  await page.getByLabel("Confirm new password (required)").fill("something-else-entirely");
  await page.getByRole("button", { name: "Save the new password" }).click();
  await expect(page.getByLabel("Confirm new password (required)")).toHaveAttribute("aria-invalid", "true");
  // The API's validators answer inline, and the link stays usable.
  await page.getByLabel("New password (required)", { exact: true }).fill("short");
  await page.getByLabel("Confirm new password (required)").fill("short");
  await page.getByRole("button", { name: "Save the new password" }).click();
  await expect(page.getByLabel("New password (required)", { exact: true })).toHaveAttribute("aria-invalid", "true");
  await page.getByLabel("New password (required)", { exact: true }).fill(SECOND);
  await page.getByLabel("Confirm new password (required)").fill(SECOND);
  await page.getByRole("button", { name: "Save the new password" }).click();

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("status").filter({ hasText: "Your password is changed" })).toBeVisible();
  await expectNoViolations(page, "Sign in after a reset");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(SECOND);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();

  // The session from before the reset ended: its next request asks to sign in again.
  await oldPage.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications" }).click();
  await expect(oldPage.getByRole("dialog", { name: "Your session ended" })).toBeVisible();

  // The link works once.
  const again = await (await browser.newContext({ baseURL })).newPage();
  await again.goto(link!);
  await again.getByLabel("New password (required)", { exact: true }).fill(`${SECOND}-again`);
  await again.getByLabel("Confirm new password (required)").fill(`${SECOND}-again`);
  await again.getByRole("button", { name: "Save the new password" }).click();
  await expect(again.getByRole("heading", { level: 1, name: "This link can't be used" })).toBeVisible();
  await expect(again.getByRole("link", { name: "Ask for a new link" })).toHaveAttribute("href", "/forgot-password");
  await expectNoViolations(again, "Reset link that can't be used");

  await Promise.all([again.context().close(), page.context().close(), old.close(), admin.context().close()]);
});

test("While platform staff impersonate a member, asking for a reset link shows the refusal", async ({ browser, baseURL }) => {
  const staff = await (await browser.newContext({ baseURL })).newPage();
  await staff.goto("/login");
  await staff.getByLabel("Email").fill("platform@example.test");
  await staff.getByLabel("Password").fill(PASSWORD);
  await staff.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Organizations" })).toBeVisible();
  const orgs: { results: { id: string; slug: string }[] } = await (await staff.request.get("/api/v1/platform/organizations?q=contoso-synthetic")).json();
  const orgId = orgs.results.find((o) => o.slug === "contoso-synthetic")!.id;
  const detail: { members: { id: number; email: string }[] } = await (await staff.request.get(`/api/v1/platform/organizations/${orgId}`)).json();
  const viewer = detail.members.find((m) => m.email === "viewer@example.test")!.id;
  await post(staff, "platform/impersonation", { organization_id: orgId, user_id: viewer, reason: "Password reset refusal test (synthetic).", password: PASSWORD });

  await staff.goto("/forgot-password");
  await staff.getByLabel("Email (required)").fill("viewer@example.test");
  await staff.getByRole("button", { name: "Email me a link" }).click();
  await expect(staff.getByRole("alert")).toContainText("nothing was sent or changed");
  await expect(staff.getByRole("heading", { level: 1, name: "Check your email" })).toHaveCount(0);
  await post(staff, "platform/impersonation", undefined, "DELETE");
  await staff.context().close();
});
