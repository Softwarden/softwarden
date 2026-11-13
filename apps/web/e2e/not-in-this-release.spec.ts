import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs } from "./helpers";

/** SAML single sign-on and SCIM provisioning come from an optional package that isn't part of this repository, so no
 *  organization has them: the sign-in settings don't offer SAML, the navigation has no Provisioning, and the
 *  Provisioning page opened by its address says it isn't part of this release, offering nothing. axe on each page. */

test("SAML and SCIM aren't offered: no SAML card, no Provisioning, and the page says why", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const me: { capabilities: string[] } = await (await admin.request.get("/api/v1/auth/me")).json();
  for (const name of ["sso.saml", "scim"]) expect(me.capabilities).not.toContain(name);

  await admin.goto("/admin/sign-in");
  await expect(admin.getByRole("heading", { level: 1, name: "Single sign-on" })).toBeVisible();
  await expect(admin.getByRole("radio", { name: /^Microsoft Entra ID/ })).toBeVisible();
  await expect(admin.getByRole("radio", { name: /^SAML/ })).toHaveCount(0);
  await expectNoViolations(admin, "Single sign-on without SAML");

  await expect(admin.getByRole("navigation").getByRole("link", { name: "Provisioning", exact: true })).toHaveCount(0);
  await admin.goto("/admin/provisioning");
  const notice = admin.locator("[data-unavailable]");
  await expect(notice).toContainText("SCIM provisioning isn't available.");
  await expect(notice).toContainText("planned for a later Enterprise edition and isn't part of this release");
  await expect(admin.getByRole("button", { name: /token/i })).toHaveCount(0);
  await expectNoViolations(admin, "Provisioning, not in this release");

  // The SCIM service isn't served: the address falls through to the app.
  const scim = await admin.request.get("/scim/v2/no-such-org/Users");
  expect(scim.headers()["content-type"] ?? "").not.toContain("application/scim+json");
  await admin.context().close();
});
