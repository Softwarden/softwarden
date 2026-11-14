import { expect, test, type Browser, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { emailedInvitation, pageAs, post } from "./helpers";

/** The invitation link's page, /invite#<token> (access and provisioning; the token in the fragment, kept in
 *  sessionStorage across a sign-in round trip back to /invite). admin.spec.ts covers the Admin's
 *  dialog and a new account accepting. Here: an account that already exists signs in first and then joins; a browser
 *  signed in to another account is asked to sign out; and a link that can't be used gets one answer. axe runs in light
 *  and dark at each state (e2e/axe.ts). The invitee opens the link from the invitation email (the e2e outbox): with
 *  more than one organization on the server, the link the Admin is shown says nothing about the address and creates no
 *  account. */

const PASSPHRASE = "synthetic-invitee-passphrase";
const tokenOf = (link: string) => link.split("/invite#")[1];

/** A brand-new account in the synthetic Litware enterprise organization (made by its Admin's invitation, accepted
 *  through the API), so it exists but isn't a member of Contoso. */
async function accountElsewhere(browser: Browser, baseURL: string | undefined, contoso: Page, email: string) {
  const { email: litwareAdmin } = await post(contoso, "e2e/enterprise-org", { which: "secondary" });
  const litware = await pageAs(browser, baseURL, litwareAdmin);
  await post(litware, "invitations", { email, role: "viewer", name: "Existing Person" });
  await litware.context().close();
  const link = await emailedInvitation(contoso, email); // only the emailed link creates an account
  const context = await browser.newContext({ baseURL });
  await context.request.get("/api/v1/auth/csrf");
  const csrf = (await context.cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const res = await context.request.post("/api/v1/auth/invitations/accept", { data: { token: tokenOf(link), name: "Existing Person", password: PASSPHRASE }, headers: { "X-CSRFToken": csrf } });
  expect(res.ok(), await res.text()).toBeTruthy();
  await context.close();
}

test("An existing account signs in from the invitation, joins, and a browser signed in as someone else is asked to sign out", async ({ browser, baseURL }) => {
  const stamp = Date.now();
  const email = `existing.${stamp}@example.test`;
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  await accountElsewhere(browser, baseURL, contoso, email);
  await post(contoso, "invitations", { email, role: "editor" });
  const link = await emailedInvitation(contoso, email);

  // Nobody signed in: sign in first, coming back to the invitation.
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto(`/invite#${tokenOf(link)}`);
  await expect(page.getByRole("heading", { level: 1, name: "Join Contoso (synthetic)" })).toBeVisible();
  await expect(page).toHaveURL(/\/invite$/);
  await expect(page.getByRole("heading", { level: 2, name: "Sign in to accept" })).toBeVisible();
  await expect(page.getByText("Editor", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Password (required)", { exact: true })).toHaveCount(0);
  await expectNoViolations(page, "Invitation page, sign in to accept");
  await page.getByRole("link", { name: "Sign in with your password" }).click();
  await expect(page).toHaveURL(/\/login\?next=%2Finvite$/);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSPHRASE);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  // Back on the invitation (the tab kept the token), signed in as the invited account: Join.
  await expect(page).toHaveURL(/\/invite$/);
  const join = page.getByRole("button", { name: "Join Contoso (synthetic)" });
  await expect(join).toBeVisible();
  await expectNoViolations(page, "Invitation page, signed in as the invitee");
  await join.click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Organization\s*:\s*Contoso \(synthetic\)\s*\.\s*Switch organization/ })).toBeVisible();
  const members: { email: string; role: string }[] = await (await contoso.request.get("/api/v1/users")).json();
  expect(members.find((m) => m.email === email)?.role).toBe("editor");

  // The same browser opens an invitation meant for someone else: sign out first, then create that account here.
  const newcomer = `newcomer.${stamp}@example.test`;
  await post(contoso, "invitations", { email: newcomer, role: "viewer" });
  await page.goto(await emailedInvitation(contoso, newcomer));
  await expect(page.getByRole("heading", { level: 2, name: "A different account is signed in" })).toBeVisible();
  await expect(page.getByText(/This invitation is for n\*\*\*@example\.test/)).toBeVisible();
  await expectNoViolations(page, "Invitation page, another account signed in");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { level: 2, name: "Create your account" })).toBeVisible();
  await expect(page.getByLabel("Your name (required)")).toBeVisible();
  await page.context().close();
  await contoso.context().close();
});

test("The link the Admin is shown can't create an account: it offers signing in and points a newcomer to the invitation email, whose link creates the account", async ({ browser, baseURL }) => {
  const email = `copied.${Date.now()}@example.test`;
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const invited = await post(contoso, "invitations", { email, role: "viewer" });
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto(`/invite#${tokenOf(invited.link)}`);
  await expect(page.getByRole("heading", { level: 1, name: "Join Contoso (synthetic)" })).toBeVisible();
  // No create form: the copied link says nothing about the address and can't make its account.
  await expect(page.getByRole("heading", { level: 2, name: "Sign in to accept" })).toBeVisible();
  await expect(page.getByText(/If c\*\*\*@example\.test already has a Softwarden account/)).toBeVisible();
  await expect(page.getByTestId("use-invitation-email")).toContainText("This copy of the link can’t create an account. Open the link in the invitation email");
  await expect(page.getByRole("heading", { level: 2, name: "Create your account" })).toHaveCount(0);
  await expect(page.getByLabel("Password (required)", { exact: true })).toHaveCount(0);
  await expectNoViolations(page, "Invitation page, copied link");
  // The emailed link proves the mailbox: it creates the account. (A fresh load: going from /invite to /invite#token
  // only changes the fragment, which a page already open has read.)
  await page.goto("/login");
  await page.goto(await emailedInvitation(contoso, email));
  await expect(page.getByRole("heading", { level: 2, name: "Create your account" })).toBeVisible();
  await expect(page.getByTestId("use-invitation-email")).toHaveCount(0);
  await page.getByLabel("Password (required)", { exact: true }).fill(PASSPHRASE);
  await page.getByLabel("Confirm password (required)").fill(PASSPHRASE);
  await page.getByLabel("Your name (required)").fill("Copied Person");
  await page.getByRole("button", { name: "Create account and join Contoso (synthetic)" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await page.context().close();
  await contoso.context().close();
});

test("A link that can't be used gets one answer, in light and dark", async ({ browser, baseURL }) => {
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto("/invite#not-a-real-invitation-token");
  await expect(page.getByRole("heading", { level: 1, name: "This invitation link can't be used" })).toBeVisible();
  await expect(page.getByText("Ask the administrator who invited you to send a new link.", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  await expectNoViolations(page, "Invitation page, unusable link");

  // A revoked link reads the same.
  const contoso = await pageAs(browser, baseURL, "admin@example.test");
  const invited = await post(contoso, "invitations", { email: `revoked.${Date.now()}@example.test`, role: "viewer" });
  await post(contoso, `invitations/${invited.id}/revoke`, {});
  await page.goto(`/invite#${tokenOf(invited.link)}`);
  await expect(page.getByRole("heading", { level: 1, name: "This invitation link can't be used" })).toBeVisible();
  // An unusable token isn't kept: /invite with nothing in the fragment reads the same.
  expect(await page.evaluate(() => sessionStorage.getItem("softwarden.invitation"))).toBeNull();
  await page.goto("/invite");
  await expect(page.getByRole("heading", { level: 1, name: "This invitation link can't be used" })).toBeVisible();
  await contoso.context().close();
  await page.context().close();
});
