import { expect, test, type Browser, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PASSWORD, pageAs, post } from "./helpers";

/** While platform staff impersonate an Admin, the sign-in settings the API refuses
 *  (auto-provisioning: 403 impersonation-restricted) are shown but can't be changed, with the reason. Each impersonation is this browser session's own, so nothing else in the suite sees it.
 *  axe in light and dark. */

const WHY = "Sign-in settings stay with this organization's Admins: they can't be changed while you impersonate someone. Stop impersonating to change them as yourself.";

async function staffSession(browser: Browser, baseURL: string | undefined): Promise<Page> {
  const staff = await (await browser.newContext({ baseURL })).newPage();
  await staff.goto("/login");
  await staff.getByLabel("Email").fill("platform@example.test");
  await staff.getByLabel("Password").fill(PASSWORD);
  await staff.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(staff.getByRole("heading", { level: 1, name: "Organizations" })).toBeVisible();
  return staff;
}

/** Impersonate `email` in organization `organizationId` through the console's API, then open `path`. */
async function impersonate(staff: Page, organizationId: string, email: string, path: string) {
  const detail: { members: { id: number; email: string }[] } = await (await staff.request.get(`/api/v1/platform/organizations/${organizationId}`)).json();
  const userId = detail.members.find((m) => m.email === email)!.id;
  await post(staff, "platform/impersonation", { organization_id: organizationId, user_id: userId, reason: "Sign-in settings test (synthetic).", password: PASSWORD });
  await staff.goto(path);
  await expect(staff.getByTestId("impersonation-banner")).toBeVisible();
}

test("Impersonating an Admin, Organization settings show auto-provisioning turned off for changes, with the reason", async ({ browser, baseURL }) => {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "e2e/sso-seed", {}); // Contoso's single sign-on is active, so its own Admins may change the switch
  const organizationId = ((await (await contoso.request.get("/api/v1/organization")).json()) as { id: string }).id;
  // The Admin, as themselves: the switch can be changed.
  await contoso.goto("/admin/organization");
  const own = contoso.getByRole("checkbox", { name: "Let people without an account ask for access" });
  await expect(own).toBeEnabled();
  await contoso.context().close();

  const staff = await staffSession(browser, baseURL);
  await impersonate(staff, organizationId, "admin@example.test", "/admin/organization");
  const auto = staff.getByRole("checkbox", { name: "Let people without an account ask for access" });
  await expect(auto).toBeDisabled();
  await expect(staff.getByTestId("sso-auto-provision-why")).toHaveText(WHY);
  await expect(auto).toHaveAccessibleDescription(new RegExp(WHY.slice(0, 40).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  await expectNoViolations(staff, "Organization settings while impersonating");
  // The other settings still save (the API allows them).
  await staff.getByRole("button", { name: "Save settings" }).click();
  await expect(staff.getByRole("status").filter({ hasText: "Settings saved." })).toBeAttached();
  await post(staff, "platform/impersonation", undefined, "DELETE");
  await staff.context().close();
});
