import { expect, test, type Browser, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { emailedInvitation, pageAs, post } from "./helpers";

/** "An organisation can sign in with Entra and another with Okta on the same instance; a
 *  broken draft configuration cannot be activated until its test passes."
 *
 *  Single sign-on settings, sign-in domains, auto-provision and pending access are organization-wide, so these specs use
 *  the synthetic Fourth Coffee organization (e2e/spec-org `sso`, used by these specs only) rather than
 *  Contoso, whose Entra settings login.spec.ts and discovery.spec.ts rely on. Every identity provider is the API's
 *  simulator (core/oidc_simulator.py): the Okta-shaped issuer is okta.simulated.invalid with the `default`
 *  authorization server, and the client secret `rejected-secret` is refused at its token endpoint. axe runs in light and
 *  dark at each state (e2e/axe.ts). */

test.describe.configure({ mode: "serial", timeout: 180_000 });

const ADMIN = "sso-admin@example.test";
const EDITOR = "sso-editor@example.test";
const OKTA_DOMAIN = "okta.simulated.invalid";
const CLIENT_ID = "11111111-2222-4333-8444-555555555555";
const SECRET = "synthetic-oidc-secret";
const REJECTED = "rejected-secret";

let slug = "";

async function proseware(browser: Browser, baseURL: string | undefined): Promise<Page> {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "e2e/spec-org", { which: "sso" });
  await contoso.context().close();
  const admin = await pageAs(browser, baseURL, ADMIN);
  slug = ((await (await admin.request.get("/api/v1/organization")).json()) as { slug: string }).slug;
  return admin;
}

/** At the simulated identity provider's page: sign in as `email`. */
async function atProvider(page: Page, email: string, shape?: string) {
  await expect(page.getByRole("heading", { name: "Simulated identity provider" })).toBeVisible();
  if (shape) await expect(page.getByText(`(${shape}-shaped)`)).toBeVisible();
  await page.getByLabel("Sign in as (email)").fill(email);
  await page.getByRole("button", { name: "Continue" }).click();
}

test("An Okta draft can't be activated until its test sign-in passes; a broken draft fails its test and stays unactivatable", async ({ browser, baseURL }) => {
  const admin = await proseware(browser, baseURL);
  await admin.goto("/admin/sign-in");
  await expect(admin.getByRole("heading", { level: 1, name: "Single sign-on" })).toBeVisible();
  // Nothing set up here yet: the legacy state, no provider, a named gap for what the draft needs.
  await expect(admin.getByTestId("sso-status-chip")).toHaveText("○Legacy (not set up here)");
  await expect(admin.getByText("None activated")).toBeVisible();
  await expect(admin.getByTestId("sso-test-result")).toContainText("Not tested");
  await expect(admin.getByRole("list", { name: "What the draft needs" })).toContainText("No provider");
  await expect(admin.getByTestId("sso-redirect-uri")).toHaveText(/\/api\/v1\/auth\/oidc\/callback$/);
  await expect(admin.getByTestId("sso-sign-in-link")).toHaveText(new RegExp(`/login\\?org=${slug}$`));
  const activate = admin.getByRole("button", { name: "Activate draft" });
  await expect(activate).toBeDisabled();
  await expectNoViolations(admin, "Single sign-on, nothing set up");

  // An Okta-shaped draft: the preset's fields only; the secret is write-only.
  await admin.getByRole("radio", { name: /^Okta/ }).check();
  await expect(admin.getByLabel("Directory (tenant) ID (required)")).toHaveCount(0);
  await admin.getByLabel("Okta domain (required)").fill(OKTA_DOMAIN);
  await admin.getByLabel("Authorization server ID").fill("default");
  await admin.getByLabel("Client ID (required)").fill(CLIENT_ID);
  await admin.getByLabel("Client secret (required)").fill(SECRET);
  await expect(admin.getByTestId("sso-dirty")).toHaveText("Unsaved changes");
  await expect(activate).toBeDisabled();
  await expect(admin.getByTestId("sso-activate-why")).toHaveText("Save and test your changes before activating.");
  await admin.getByRole("button", { name: "Save draft" }).click();
  await expect(admin.getByTestId("sso-status")).toContainText("Draft saved as revision 1. Test it before activating.");
  await expect(admin.getByRole("heading", { level: 2, name: "Draft (revision 1)" })).toBeVisible();
  // The stored secret is never shown: since when, and Replace.
  await expect(admin.getByTestId("sso-secret")).toHaveText(/^✓Set on \d{4}-\d{2}-\d{2}$/);
  await expect(admin.getByLabel("Client secret (required)")).toHaveCount(0);
  await expect(admin.getByText(SECRET)).toHaveCount(0);
  await expect(admin.getByRole("list", { name: "What the draft needs" })).toHaveCount(0);
  await expect(activate).toBeDisabled();
  await expect(admin.getByTestId("sso-activate-why")).toHaveText("Activate needs a passed test of revision 1. It hasn't been tested.");
  await expectNoViolations(admin, "Single sign-on, Okta draft saved");

  // The test is a real sign-in with the Admin's own account at the simulator, coming back to this page.
  await admin.getByRole("button", { name: "Test sign-in" }).click();
  await atProvider(admin, ADMIN, "Okta");
  await expect(admin).toHaveURL(/\/admin\/sign-in$/); // the result is read, then taken out of the address
  await expect(admin.getByTestId("sso-status")).toContainText("Test sign-in passed for revision 1. You can activate this draft now.");
  await expect(admin.getByTestId("sso-test-result")).toContainText("Passed");
  await expect(admin.getByTestId("sso-test-result")).toContainText("Revision 1 by Sam Admin");
  await expect(activate).toBeEnabled();
  await expectNoViolations(admin, "Single sign-on, test passed");

  await activate.click();
  await expect(admin.getByTestId("sso-status")).toContainText("Single sign-on is active with Okta (revision 1).");
  await expect(admin.getByTestId("sso-status-chip")).toHaveText("✓Active");
  await expect(admin.getByTestId("sso-active-provider")).toHaveText("Okta");
  await expect(admin.getByRole("region", { name: "Sign-in now" }).getByText(/^Sam Admin on \d{4}-\d{2}-\d{2}/)).toBeVisible();
  await expect(activate).toBeDisabled();
  await expect(admin.getByTestId("sso-activate-why")).toHaveText("Revision 1 is already the active configuration.");

  // A broken draft: a client secret the provider refuses. Test saves first, then fails with a code.
  await admin.getByRole("button", { name: "Replace the client secret" }).click();
  await admin.getByLabel("New client secret").fill(REJECTED);
  await admin.getByRole("button", { name: "Save and test sign-in" }).click();
  await atProvider(admin, ADMIN);
  await expect(admin).toHaveURL(/\/admin\/sign-in$/);
  await expect(admin.getByTestId("sso-status")).toContainText("Test sign-in failed (token_refused).");
  await expect(admin.getByTestId("sso-status")).toContainText("The draft can't be activated until a test passes.");
  await expect(admin.getByRole("heading", { level: 2, name: "Draft (revision 2)" })).toBeVisible();
  await expect(admin.getByTestId("sso-test-result")).toContainText("Failed");
  await expect(admin.getByTestId("sso-test-code")).toHaveText("token_refused");
  await expect(activate).toBeDisabled();
  await expect(admin.getByTestId("sso-activate-why")).toHaveText("Activate needs a passed test of revision 2. Its last test failed.");
  // Sign-in still uses the active, working configuration.
  await expect(admin.getByTestId("sso-status-chip")).toHaveText("✓Active");
  await expectNoViolations(admin, "Single sign-on, broken draft failed its test");

  // Activating it through the API is refused too (the button isn't the only guard).
  const csrf = (await admin.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const refused = await admin.request.post("/api/v1/sso/settings/activate", { data: { draft_revision: 2 }, headers: { "X-CSRFToken": csrf } });
  expect(refused.status()).toBe(409);
  expect((await refused.json()).type).toBe("/problems/sso-test-required");

  // A change after a test makes the result belong to an earlier revision.
  await admin.getByLabel("Client ID (required)").fill(`${CLIENT_ID}`.replace(/5$/, "6"));
  await admin.getByRole("button", { name: "Save draft" }).click();
  await expect(admin.getByRole("heading", { level: 2, name: "Draft (revision 3)" })).toBeVisible();
  await expect(admin.getByTestId("sso-test-result")).toContainText("Tested an earlier revision");
  await expect(admin.getByTestId("sso-activate-why")).toHaveText("Activate needs a passed test of revision 3. The last test was of revision 2.");

  // Another Okta domain sends sign-in elsewhere, so the stored secret isn't kept for it: its field opens,
  // without "Keep", and the API's refusal on it links to that field.
  await expect(admin.getByLabel(/client secret/i)).toHaveCount(0);
  await admin.getByLabel("Okta domain (required)").fill("other.okta.simulated.invalid");
  const secret = admin.getByLabel("Client secret (required)");
  await expect(secret).toHaveValue("");
  await expect(admin.getByText("Sign-in now goes to another address, so the stored secret isn't sent there")).toBeVisible();
  await expect(admin.getByRole("button", { name: "Keep the stored secret" })).toHaveCount(0);
  // A secret typed there and then hidden again (the domain changed back) is never sent: nothing is left to save.
  await secret.fill("typed-then-hidden");
  await admin.getByLabel("Okta domain (required)").fill(OKTA_DOMAIN);
  await expect(admin.getByLabel(/client secret/i)).toHaveCount(0);
  await expect(admin.getByTestId("sso-dirty")).toHaveCount(0);
  const patches: string[] = [];
  admin.on("request", (r) => { if (r.method() === "PATCH" && r.url().endsWith("/api/v1/sso/settings")) patches.push(r.postData() ?? ""); });
  await admin.getByRole("button", { name: "Save draft" }).click();
  await expect(admin.getByTestId("sso-status")).toContainText("Nothing changed in the draft.");
  expect(patches).toEqual([]);
  await admin.getByLabel("Okta domain (required)").fill("other.okta.simulated.invalid");
  await secret.fill("");
  await admin.getByRole("button", { name: "Save draft" }).click();
  const summary = admin.getByRole("alert").filter({ hasText: "Fix this field" });
  await summary.getByRole("link").click();
  await expect(secret).toBeFocused();
  await expectNoViolations(admin, "Single sign-on, a new Okta domain needs the secret again");
  // Back to the stored domain: the secret is kept again, and Replace is back.
  await admin.getByLabel("Okta domain (required)").fill(OKTA_DOMAIN);
  await expect(admin.getByRole("button", { name: "Keep the stored secret" })).toBeVisible();
  await admin.context().close();
});

test("One organization signs in with Entra and another with Okta on the same instance", async ({ browser, baseURL }) => {
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const { org: contosoSlug } = (await post(contoso, "e2e/sso-seed", {})) as { org: string };
  await contoso.context().close();
  expect(slug).not.toBe("");

  // Contoso: Microsoft Entra ID (the simulated tenant).
  const entra = await (await browser.newContext({ baseURL })).newPage();
  await entra.goto(`/login?${new URLSearchParams({ org: contosoSlug })}`);
  await entra.getByRole("link", { name: "Sign in with Microsoft Entra ID" }).click();
  await atProvider(entra, "editor@example.test", "Entra");
  await expect(entra.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(entra.getByRole("button", { name: /Sam Editor · Editor/ })).toBeVisible();
  await entra.context().close();

  // Fourth Coffee: Okta, activated through the settings page above.
  const okta = await (await browser.newContext({ baseURL })).newPage();
  await okta.goto(`/login?${new URLSearchParams({ org: slug })}`);
  await expect(okta.getByRole("link", { name: "Sign in with Microsoft Entra ID" })).toHaveCount(0);
  await expectNoViolations(okta, "Sign-in with the Okta button");
  await okta.getByRole("link", { name: "Sign in with Okta" }).click();
  await atProvider(okta, EDITOR, "Okta");
  await expect(okta.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(okta.getByRole("button", { name: /Sid Editor · Editor/ })).toBeVisible();
  await okta.context().close();
});

test("Auto-provision puts an unknown identity in pending access; approving it creates the member, whose identity can be unlinked", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, ADMIN);
  const stamp = Date.now();
  const newcomer = `newcomer.${stamp}@fourthcoffee.example.test`;

  // Organization settings: the switch, explained, now that single sign-on is active.
  await admin.goto("/admin/organization");
  const auto = admin.getByRole("checkbox", { name: "Let people without an account ask for access" });
  await expect(auto).toBeEnabled();
  await expect(auto).not.toBeChecked();
  await expect(admin.getByTestId("sso-auto-provision-why")).toHaveCount(0);
  await auto.check();
  await admin.getByRole("button", { name: "Save settings" }).click();
  await expect(admin.getByRole("status").filter({ hasText: "Auto-provision is on" })).toBeAttached();
  await expectNoViolations(admin, "Organization settings with auto-provision");

  // Someone without an account signs in through Fourth Coffee's Okta: they wait, signed in nowhere.
  const stranger = await (await browser.newContext({ baseURL })).newPage();
  for (const email of [newcomer, "viewer@example.test"]) {
    await stranger.goto(`/login?${new URLSearchParams({ org: slug })}`);
    await stranger.getByRole("link", { name: "Sign in with Okta" }).click();
    await atProvider(stranger, email);
    await expect(stranger).toHaveURL(/\/login\?error=access_pending/);
    await expect(stranger.getByRole("alert")).toContainText("Your request for access is waiting for a Softwarden administrator");
  }
  await stranger.context().close();

  // Users & roles › Pending access: search in the URL, approve with a role.
  await admin.goto("/admin/users");
  await admin.getByRole("tab", { name: "Pending access" }).click();
  await expect(admin).toHaveURL(/\/admin\/users\?tab=pending$/);
  const table = admin.getByRole("region", { name: "Pending access table" });
  await expect(table.getByRole("rowheader", { name: newcomer })).toBeVisible();
  await expect(table.getByRole("row").filter({ hasText: newcomer })).toContainText("Okta");
  await expectNoViolations(admin, "Pending access");
  await admin.getByLabel("Search").fill(`newcomer.${stamp}`);
  await expect(admin).toHaveURL(new RegExp(`q=newcomer\\.${stamp}`));
  await expect(table.getByRole("row")).toHaveCount(2); // the header and the one match
  await table.getByRole("button", { name: `Approve ${newcomer}` }).click();
  const approve = admin.getByRole("dialog", { name: `Approve ${newcomer}` });
  await approve.getByLabel("Role").selectOption("editor");
  await expectNoViolations(admin, "Approve pending access");
  await approve.getByRole("button", { name: "Approve" }).click();
  await expect(approve).toHaveCount(0);
  await expect(admin.getByRole("status").filter({ hasText: `${newcomer} can sign in now, as Editor.` })).toBeAttached();
  await expect(admin.getByText("No requests match these filters")).toBeVisible();

  // Another organization invites the address. Its emailed link (only the mailbox's owner holds it) says
  // that Fourth Coffee made an account for it, that creating one retires it, and where to sign in to keep it.
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "invitations", { email: newcomer, role: "viewer" });
  const owner = await (await browser.newContext({ baseURL })).newPage();
  const ownerLink = await emailedInvitation(contoso, newcomer);
  await owner.goto(ownerLink);
  await expect(owner.getByRole("heading", { level: 2, name: "Create your account" })).toBeVisible();
  const made = owner.getByTestId("organization-account");
  await expect(made).toContainText("already made an account for this address through its single sign-on. Creating an account here retires that one");
  await expect(made.getByRole("link", { name: /^sign in through / })).toHaveAttribute("href", `/login?org=${slug}`);
  await expectNoViolations(owner, "Invitation page, an account an organization made");
  await owner.context().close();

  // An account that belongs to another organization needs an invitation instead (409 invitation-required).
  await admin.getByRole("button", { name: "Clear filters" }).first().click();
  await table.getByRole("button", { name: "Approve viewer@example.test" }).click();
  const refuse = admin.getByRole("dialog", { name: "Approve viewer@example.test" });
  await refuse.getByRole("button", { name: "Approve" }).click();
  await expect(refuse.getByRole("alert")).toContainText("Invite the person instead");
  await expectNoViolations(admin, "Approve refused: invitation required");
  await refuse.getByRole("button", { name: "Invite instead" }).click();
  const invite = admin.getByRole("dialog", { name: "Invite user" });
  await expect(invite.getByLabel("Email (required)")).toHaveValue("viewer@example.test");
  await invite.getByRole("button", { name: "Cancel" }).click();
  await expect(invite).toHaveCount(0);
  // "Invite instead" was inside the approve dialog, gone with it: focus returns to the row's Approve button.
  await expect(table.getByRole("button", { name: "Approve viewer@example.test" })).toBeFocused();
  // Decline it, behind a confirmation.
  await table.getByRole("button", { name: "Decline viewer@example.test" }).click();
  const decline = admin.getByRole("dialog", { name: "Decline viewer@example.test?" });
  await decline.getByRole("button", { name: "Decline viewer@example.test" }).click();
  await expect(decline).toHaveCount(0);
  await expect(table.getByRole("rowheader", { name: "viewer@example.test" })).toHaveCount(0);

  // The new member signs in with Okta; their identity is listed and can be unlinked, behind a confirmation.
  await admin.getByRole("tab", { name: "Members" }).click();
  await expect(admin).toHaveURL(/\/admin\/users$/);
  const row = admin.getByRole("region", { name: "Users table" }).getByRole("row").filter({ hasText: newcomer });
  await expect(row).toContainText("Signs in with Okta");
  await expect(row).toContainText(`https://${OKTA_DOMAIN}/oauth2/default`);
  // An account approval made has no password, so there is none to reset (the API refuses it): no button, why.
  await expect(row.getByRole("button", { name: /^Reset password/ })).toHaveCount(0);
  await expect(row.getByTestId("no-password")).toHaveText("No password to reset: signs in through single sign-on");
  await expect(admin.getByRole("region", { name: "Users table" }).getByRole("row").filter({ hasText: ADMIN }).getByRole("button", { name: /^Reset password/ })).toBeVisible();
  await expectNoViolations(admin, "Members with single sign-on identities");
  await row.getByRole("button", { name: /^Unlink Okta/ }).click();
  const unlink = admin.getByRole("dialog", { name: /^Unlink Okta from / });
  await expect(unlink).toContainText(`https://${OKTA_DOMAIN}/oauth2/default`);
  await expectNoViolations(admin, "Unlink an identity");
  await unlink.getByRole("button", { name: "Unlink Okta" }).click();
  await expect(unlink).toHaveCount(0);
  // With no password either, it has nothing left to sign in with: a named gap, not "Password only".
  await expect(row).toContainText("No way to sign in");
  await expect(row).not.toContainText("Password only");
  await expect(row).not.toContainText("Signs in with Okta");

  // The address's owner creates an account from Contoso's emailed invitation; Fourth Coffee's account is retired
  // and its audit log says so in words.
  const claimer = await (await browser.newContext({ baseURL })).newPage();
  await claimer.goto(ownerLink);
  await claimer.getByLabel("Your name (required)").fill("Real Newcomer");
  await claimer.getByLabel("Password (required)", { exact: true }).fill("synthetic-owner-passphrase");
  await claimer.getByLabel("Confirm password (required)").fill("synthetic-owner-passphrase");
  await claimer.getByRole("button", { name: /^Create account and join / }).click();
  await expect(claimer.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await claimer.context().close();
  await contoso.context().close();
  await admin.goto("/admin/audit?action=user.address_reclaimed");
  await expect(admin.getByRole("main")).toContainText("Account retired: its address's owner signed up or accepted an invitation");
  await admin.context().close();
});

test("An invitation opened in a browser signed in through another organization's single sign-on offers sign-out, then the sign-in choices", async ({ browser, baseURL }) => {
  // Sid Editor signs in through Fourth Coffee's Okta: a session confined to Fourth Coffee.
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto(`/login?${new URLSearchParams({ org: slug })}`);
  await page.getByRole("link", { name: "Sign in with Okta" }).click();
  await atProvider(page, EDITOR);
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();

  // Contoso invites the same account.
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await post(contoso, "invitations", { email: EDITOR, role: "viewer" });
  // The emailed link: with more than one organization on the server only it says the address has an account.
  const link = await emailedInvitation(contoso, EDITOR);
  await contoso.context().close();
  const tokenOf = (l: string) => l.split("/invite#")[1];

  // The API reports such a session as `other_sign_in`.
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const preview = await page.request.post("/api/v1/auth/invitations/preview", { data: { token: tokenOf(link) }, headers: { "X-CSRFToken": csrf } });
  const session = ((await preview.json()) as { session?: string }).session;
  expect(session).toBe("other_sign_in");

  await page.goto(`/invite#${tokenOf(link)}`);
  await expect(page.getByRole("heading", { level: 2, name: "Signed in through another organization" })).toBeVisible();
  await expect(page.getByText(/signed in through another organization's single sign-on/)).toBeVisible();
  await expectNoViolations(page, "Invitation page, signed in through another organization");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Sign in to accept" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in with your password" })).toBeVisible();
  await page.context().close();
});

test("Under SOFTWARDEN_FORCE_LOCAL_LOGIN the page says single sign-on is off on this installation, and why test and activate are refused", async ({ browser, baseURL }) => {
  // The e2e API server can't change its environment mid-run, so the settings it answers are given `force_local_login`
  // in the browser, as an installation with the variable set answers them (the rest is the real answer, ETag included).
  const admin = await proseware(browser, baseURL);
  await admin.goto("/admin/sign-in");
  await expect(admin.getByRole("heading", { level: 1, name: "Single sign-on" })).toBeVisible();
  await expect(admin.getByRole("heading", { name: /^Draft \(revision \d+\)$/ })).toBeVisible();
  await expect(admin.getByTestId("force-local")).toHaveCount(0);

  await admin.route((url) => url.pathname === "/api/v1/sso/settings", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const body = await response.json() as Record<string, unknown>;
    await route.fulfill({ response, json: { ...body, force_local_login: true } });
  });
  await admin.reload();
  const banner = admin.getByTestId("force-local");
  await expect(banner).toContainText("Single sign-on is turned off on this installation.");
  await expect(banner).toContainText("SOFTWARDEN_FORCE_LOCAL_LOGIN");
  await expect(banner).toHaveAttribute("role", "note"); // a note in the pending tone, not an error
  await expect(admin.getByText("Test sign-ins are refused while SOFTWARDEN_FORCE_LOCAL_LOGIN is set.")).toBeVisible();
  await expect(admin.getByRole("button", { name: /Test sign-in$/ })).toBeDisabled();
  await expect(admin.getByTestId("sso-activate-why")).toHaveText("Activation is refused while SOFTWARDEN_FORCE_LOCAL_LOGIN is set.");
  await expectNoViolations(admin, "Single sign-on under SOFTWARDEN_FORCE_LOCAL_LOGIN");
  await admin.context().close();
});
