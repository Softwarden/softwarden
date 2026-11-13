import { expectNoViolations } from "./axe";
import { expect, test } from "@playwright/test";
import { pageAs, post } from "./helpers";

/** SSO settings and generic OIDC: the sign-in page asks `auth/options` for the organization a sign-in link
 *  names (?org=<slug>) and draws its single sign-on button; with several organizations and none named it offers the
 *  password form only. The password form is always there. Refusal codes from the callback read as sentences; unknown
 *  codes get the generic one. Nothing contacts a real identity provider (the seed organization uses the simulator). */

test.describe.configure({ timeout: 120_000 });

test("A sign-in link for an organization with active SSO shows its button beside the password form", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const { org } = (await post(admin, "e2e/sso-seed", {})) as { org: string };
  await admin.context().close();

  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto(`/login?${new URLSearchParams({ org, next: "/reports?tab=calendar" })}`);
  await expect(page.getByRole("heading", { name: "Sign in to Softwarden" })).toBeVisible();
  const sso = page.getByRole("link", { name: "Sign in with Microsoft Entra ID" });
  await expect(sso).toHaveAttribute("href", `/api/v1/auth/oidc/start?${new URLSearchParams({ org })}&next=${encodeURIComponent("/reports?tab=calendar")}`);
  // Password sign-in is never removed.
  await expect(page.getByRole("form", { name: "Sign in" })).toBeVisible();
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByLabel("Password")).toBeVisible();
  await expectNoViolations(page, "Sign-in for an organization with SSO");
  await page.context().close();
});

test("With several organizations and none named, and for an unknown organization, only the password form shows", async ({ browser, baseURL }) => {
  // A second organization exists (idempotent), so the installation's env Entra config no longer applies.
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await post(admin, "e2e/multi-org-user", {});
  await admin.context().close();

  const page = await (await browser.newContext({ baseURL })).newPage();
  for (const path of ["/login", "/login?org=no-such-organization"]) {
    await page.goto(path);
    await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
    await expect(page.getByLabel("Email")).toBeVisible();
    await expect(page.getByRole("link", { name: /^Sign in with/ })).toHaveCount(0);
  }
  await expectNoViolations(page, "Sign-in, password only");
  await page.context().close();
});

test("Refusal codes read as sentences; an unknown code gets the generic message; a password attempt replaces it", async ({ browser, baseURL }) => {
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto("/login?error=nonce");
  await expect(page.getByRole("alert")).toHaveText("The sign-in couldn't be matched to the one this page started. Try again from this page.");
  await page.goto("/login?error=email_unverified");
  await expect(page.getByRole("alert")).toContainText("hasn't verified your email address");
  // A sign-in waiting for an Admin's approval, and the organization's waiting list full.
  await page.goto("/login?error=access_pending&org=contoso-synthetic");
  await expect(page.getByRole("alert")).toContainText("Your request for access is waiting for a Softwarden administrator");
  await page.goto("/login?error=access_pending_full&org=contoso-synthetic");
  await expect(page.getByRole("alert")).toHaveText("Your request for access couldn't be recorded: this organization has too many requests waiting. Ask a Softwarden administrator to invite you.");
  await page.goto("/login?error=something_new");
  await expect(page.getByRole("alert")).toContainText("didn't complete. Try again, or sign in with your password.");
  // Names of Object.prototype members are unknown codes too, not a crash.
  for (const code of ["constructor", "__proto__", "toString", "hasOwnProperty"]) {
    await page.goto(`/login?error=${code}`);
    await expect(page.getByRole("alert")).toContainText("didn't complete. Try again, or sign in with your password.");
    await expect(page.getByLabel("Email")).toBeVisible();
  }
  await expectNoViolations(page, "Sign-in with a refusal");
  // A failed password attempt shows its own message instead.
  await page.getByLabel("Email").fill("nobody@example.test");
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).not.toContainText("didn't complete");
  await page.context().close();
});
