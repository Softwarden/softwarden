import { expect, test, type Page } from "@playwright/test";
import { PASSWORD } from "./helpers";

/** The platform console's second factor on the main e2e server, which keeps it off (the
 *  self-hosted default). The journey with it on (setup key, codes computed from it, impersonation) is
 *  e2e/platform-totp.spec.ts against its own server. Here:
 *  - with it off, nothing is asked, at sign-in or when impersonating;
 *  - a console page refused with 403 second-factor-required (the operator reset the factor, or this session's
 *    verification ended) shows the code page instead of that page's error. The real server can't be made to refuse
 *    mid-session here, so that refusal and the session after it are answered in the browser, as the API answers them. */

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill("platform@example.test");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Organizations" })).toBeVisible();
}

test("With the second factor off, nothing is asked at sign-in or when impersonating", async ({ browser, baseURL }) => {
  const page = await (await browser.newContext({ baseURL })).newPage();
  await signIn(page);
  await page.goto("/platform/organizations?q=contoso-synthetic");
  await page.getByRole("link", { name: "Contoso (synthetic)" }).click();
  await page.getByRole("region", { name: "Members table" }).getByRole("button", { name: "Impersonate Vic Viewer" }).click();
  const dialog = page.getByRole("dialog", { name: "Impersonate Vic Viewer" });
  await expect(dialog.getByLabel("Your current password")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Impersonate Vic Viewer" })).toBeEnabled();
  await expect(dialog.getByLabel("Code from your authenticator app (required)")).toHaveCount(0);
  await page.context().close();
});

test("A console page refused for the second factor shows the code page, not the page's error", async ({ browser, baseURL }) => {
  const page = await (await browser.newContext({ baseURL })).newPage();
  await signIn(page);
  // From now on the server answers as one whose operator reset this account's second factor.
  await page.route("**/api/v1/platform/session", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), second_factor: { required: true, enrolled: true, verified: false } } });
  });
  await page.route("**/api/v1/platform/audit-events*", (route) => route.fulfill({
    status: 403, contentType: "application/problem+json",
    json: { type: "/problems/second-factor-required", title: "Second factor required", status: 403, detail: "Enter the code from your authenticator app first.", enrolled: true },
  }));
  await page.getByRole("navigation", { name: "Platform" }).getByRole("link", { name: "Audit events" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Enter your authenticator code" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Platform" })).toHaveCount(0);
  await page.context().close();
});
