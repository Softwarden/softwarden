import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { CLOUD, CLOUD_WEB } from "./cloud-server";

/** The signup refusals the API returns read as sentences. A web address or domain name in
 *  a name is a field error at that field; the installation's daily ceiling on new organizations (503 signup-paused,
 *  SOFTWARDEN_SIGNUP_DAILY_LIMIT, 200 by default, so it is answered here as the API answers it) says signup is paused,
 *  and on the confirmation page that the link keeps working. Runs against the cloud e2e server (public signup on); it
 *  creates nothing there. */

test.skip(!CLOUD, "The cloud e2e server runs only with E2E_CLOUD=1 (playwright.config.ts).");
test.use({ baseURL: CLOUD_WEB });

const paused = (detail: string) => ({ status: 503, contentType: "application/problem+json", body: JSON.stringify({ type: "/problems/signup-paused", title: "Signup paused", status: 503, detail }) });

test("A web address in a name is refused at that field, and a paused signup says so in words", async ({ page }) => {
  const stamp = Date.now();
  await page.goto("/signup");
  await page.getByLabel("Work email (required)").fill(`refused.${stamp}@signup.example.test`);
  await page.getByLabel("Your name (required)").fill("Morgan www.evil.example");
  await page.getByLabel("Organization name (required)").fill("Reset at https://evil.example/reset");
  await page.getByLabel("Password (required)").fill("signup-refusal-passphrase-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByLabel("Organization name (required)")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByLabel("Your name (required)")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("alert").filter({ hasText: "Fix these fields" })).toContainText("Leave out web addresses and domain names (such as example.com).");
  await expect(page.getByLabel("Work email (required)")).toHaveValue(`refused.${stamp}@signup.example.test`); // nothing typed is lost
  await expectNoViolations(page, "Signup refused: a web address in a name");

  // The daily ceiling: the SPA's own sentence, whatever the server's detail.
  await page.getByLabel("Your name (required)").fill("Morgan Signup");
  await page.getByLabel("Organization name (required)").fill(`Fabrikam ${stamp} (synthetic)`);
  await page.route("**/api/v1/auth/signup", (route) => route.fulfill(paused("Signup paused.")), { times: 1 });
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "New organizations are paused for a few hours" })).toContainText("Try again later; nothing was saved.");
  await expectNoViolations(page, "Signup paused");

  // Confirming while paused: the link keeps working.
  await page.route("**/api/v1/auth/signup/verify", (route) => route.fulfill(paused("Signup paused.")), { times: 1 });
  await page.goto("/signup/verify#a-pending-signup-token");
  await page.getByLabel("Password (required)").fill("signup-refusal-passphrase-2026");
  await page.getByRole("button", { name: "Confirm and sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Your link keeps working until it expires, so open it again later. Nothing was created.");
});
