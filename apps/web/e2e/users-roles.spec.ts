import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { PASSWORD, pageAs, post } from "./helpers";

/** Users & roles' Invitations tab (status chips, filter and search in the URL, resend with a copy-once
 *  link, revoke behind a confirmation), a shared account's identities as a named gap, the single sign-on page's role
 *  gate, its nav and command palette entries, and the invitation token's one-hour expiry in this tab's storage.
 *  sso-settings.spec.ts covers the single sign-on settings, pending access and unlinking. axe in light and dark. */

test.describe.configure({ timeout: 120_000 });

const tokenOf = (link: string) => link.split("/invite#")[1];

async function previewStatus(page: Page, link: string) {
  const csrf = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  return (await page.request.post("/api/v1/auth/invitations/preview", { data: { token: tokenOf(link) }, headers: { "X-CSRFToken": csrf } })).status();
}

test("Invitations: status chips, filters in the URL, resend shows the new link once, revoke confirms", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const stamp = Date.now();
  const first = `inv.a.${stamp}@example.test`;
  const second = `inv.b.${stamp}@example.test`;
  const a = await post(admin, "invitations", { email: first, role: "viewer", name: "Avery Invitee" });
  await post(admin, "invitations", { email: second, role: "editor" });

  await admin.goto("/admin/users?tab=invitations");
  await expect(admin.getByRole("tab", { name: "Invitations", selected: true })).toBeVisible();
  await admin.getByLabel("Search").fill(String(stamp));
  await expect(admin).toHaveURL(new RegExp(`tab=invitations&q=${stamp}`));
  const table = admin.getByRole("region", { name: "Invitations table" });
  await expect(table.getByRole("row")).toHaveCount(3);
  const rowA = table.getByRole("row").filter({ hasText: first });
  await expect(rowA).toContainText("◐Pending");
  await expect(rowA).toContainText("Avery Invitee");
  await expect(rowA).toContainText("Viewer");
  await expect(rowA).toContainText("by Alex Admin");
  await expectNoViolations(admin, "Invitations tab");

  // Resend: a new link, once, in a dialog an outside click or Escape can't dismiss; the old link stops working.
  await rowA.getByRole("button", { name: `Resend the invitation for ${first}` }).click();
  // The e2e server can send email, so the new link was emailed too (`emailed`).
  const shown = admin.getByRole("dialog", { name: "New invitation link emailed" });
  await expect(shown.getByTestId("invite-note")).toBeFocused();
  const link = (await shown.getByTestId("invite-link").textContent())!;
  expect(link).toMatch(/\/invite#[\w-]{20,}$/);
  expect(link).not.toBe(a.link);
  await expectNoViolations(admin, "Resent invitation link");
  await admin.keyboard.press("Escape");
  await expect(shown).toBeVisible();
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(shown).toHaveCount(0);
  await expect(rowA.getByRole("button", { name: `Resend the invitation for ${first}` })).toBeFocused();
  await expect(admin.getByRole("status").filter({ hasText: `A new link for ${first} was made and emailed` })).toBeAttached();
  expect(await previewStatus(admin, a.link)).toBe(404);
  expect(await previewStatus(admin, link)).toBe(200);

  // Revoke, behind a confirmation naming the consequence.
  const rowB = table.getByRole("row").filter({ hasText: second });
  await rowB.getByRole("button", { name: `Revoke the invitation for ${second}` }).click();
  const confirm = admin.getByRole("dialog", { name: `Revoke the invitation for ${second}?` });
  await expectNoViolations(admin, "Revoke an invitation");
  await confirm.getByRole("button", { name: `Revoke the invitation for ${second}` }).click();
  await expect(confirm).toHaveCount(0);
  await expect(rowB).toContainText("⊘Revoked");
  await expect(rowB.getByRole("button")).toHaveCount(0);

  // The status filter is in the URL too; a shared link opens the same list.
  await admin.getByLabel("Status").selectOption("revoked");
  await expect(admin).toHaveURL(/status=revoked/);
  await expect(table.getByRole("row")).toHaveCount(2);
  await expect(table.getByRole("rowheader", { name: second })).toBeVisible();
  await admin.goto(`/admin/users?tab=invitations&status=pending&q=${stamp}`);
  await expect(admin.getByLabel("Status")).toHaveValue("pending");
  await expect(admin.getByLabel("Search")).toHaveValue(String(stamp));
  await expect(table.getByRole("rowheader", { name: first })).toBeVisible();
  await expect(table.getByRole("rowheader", { name: second })).toHaveCount(0);
  await admin.getByLabel("Search").fill(`nobody-${stamp}`);
  await expect(admin.getByText("No invitations match these filters")).toBeVisible();
  await admin.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(admin).toHaveURL(/\/admin\/users\?tab=invitations$/);
  await admin.context().close();
});

test("A shared account's identities are managed in its other organization: a named gap", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await post(admin, "e2e/multi-org-user", {});
  await admin.goto("/admin/users");
  const row = admin.getByRole("region", { name: "Users table" }).getByRole("row").filter({ hasText: "multi@example.test" });
  await expect(row).toContainText("⚠Managed in another organization");
  await expect(row).toContainText("Not shown for a shared account");
  await expect(row.getByRole("button", { name: /^Unlink/ })).toHaveCount(0);
  await expectNoViolations(admin, "Members with a shared account");
  await admin.context().close();
});

test("Single sign-on is an Admin page: in the Admin nav and the palette for Admins, explained to other roles", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/dashboard");
  await expect(admin.getByRole("group", { name: "Admin" }).getByRole("link", { name: "Single sign-on" })).toBeVisible();
  await admin.keyboard.press("Control+k");
  const palette = admin.getByRole("dialog", { name: "Command palette" });
  await palette.getByRole("combobox", { name: "Search records, pages and actions" }).fill("single sign");
  await palette.getByRole("group", { name: "Pages" }).getByRole("option", { name: /Single sign-on/ }).click();
  await expect(admin).toHaveURL(/\/admin\/sign-in$/);
  await expect(admin.getByRole("heading", { level: 1, name: "Single sign-on" })).toBeVisible();
  await admin.context().close();

  for (const [email, role] of [["editor@example.test", "an Editor"], ["viewer@example.test", "a Viewer"]] as const) {
    const page = await pageAs(browser, baseURL, email);
    await page.goto("/dashboard");
    await expect(page.getByRole("link", { name: "Single sign-on" })).toHaveCount(0);
    await page.goto("/admin/sign-in");
    await expect(page.getByTestId("admin-only")).toContainText("Only Admins can open Single sign-on.");
    await expect(page.getByTestId("admin-only")).toContainText(`You're signed in as ${role}`);
    await expect(page.getByRole("button", { name: "Activate draft" })).toHaveCount(0);
    if (role === "a Viewer") await expectNoViolations(page, "Single sign-on, not an Admin");
    await page.context().close();
  }
});

test("A kept invitation token expires after an hour", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const { link } = await post(admin, "invitations", { email: `kept.${Date.now()}@example.test`, role: "viewer" });
  await admin.context().close();
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto(`/invite#${tokenOf(link)}`);
  await expect(page.getByRole("heading", { level: 2, name: "Sign in to accept" })).toBeVisible(); // the copied link
  const kept = JSON.parse((await page.evaluate(() => sessionStorage.getItem("softwarden.invitation")))!) as { token: string; at: number };
  expect(kept.token).toBe(tokenOf(link));
  // Back in this tab within the hour: the kept token still works.
  await page.goto("/invite");
  await expect(page.getByRole("heading", { level: 2, name: "Sign in to accept" })).toBeVisible(); // the copied link
  // Kept for more than an hour: forgotten.
  await page.evaluate((t) => sessionStorage.setItem("softwarden.invitation", JSON.stringify({ token: t, at: Date.now() - 61 * 60 * 1000 })), kept.token);
  await page.goto("/invite");
  await expect(page.getByRole("heading", { level: 1, name: "This invitation link can't be used" })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("softwarden.invitation"))).toBeNull();
  await page.context().close();
});

test("Signing out of the app forgets an invitation token kept in the tab", async ({ browser, baseURL }) => {
  // Its own sign-in (not the shared session helper), because signing out ends the session.
  const page = await (await browser.newContext({ baseURL })).newPage();
  await page.goto("/login");
  await page.getByLabel("Email").fill("viewer@example.test");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await page.evaluate(() => sessionStorage.setItem("softwarden.invitation", JSON.stringify({ token: "kept-token", at: Date.now() })));
  await page.getByRole("button", { name: /· Viewer\. Account/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Softwarden" })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("softwarden.invitation"))).toBeNull();
  await page.context().close();
});

test("An invitation that wasn't emailed keeps the copy-once link: Shown only once, send it yourself", async ({ browser, baseURL }) => {
  // The e2e server can send email, so every invitation here is emailed. An installation without email (or a refused
  // message) answers `emailed: false`: the real response is passed through with only that member changed.
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.route("**/api/v1/invitations", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...(await response.json()), emailed: false } });
  });
  const email = `not.emailed.${Date.now()}@example.test`;
  await admin.goto("/admin/users");
  const opener = admin.getByRole("button", { name: "Invite user" });
  await opener.click();
  const invite = admin.getByRole("dialog", { name: "Invite user" });
  await invite.getByLabel("Email (required)").fill(email);
  await invite.getByRole("button", { name: "Invite" }).click();
  const shown = admin.getByRole("dialog", { name: "Copy the invitation link" });
  await expect(shown).toContainText("Send it to the person through a channel you trust.");
  await expect(shown.getByTestId("invite-note")).toBeFocused();
  await expect(shown.getByTestId("invite-note")).toContainText("Shown only once.");
  await expect(shown.getByTestId("invite-note")).toContainText("It wasn't emailed: send it yourself.");
  await expect(shown.getByTestId("invite-link")).toHaveText(/\/invite#[\w-]{20,}$/);
  await expectNoViolations(admin, "Invitation link dialog, not emailed");
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(opener).toBeFocused();
  await expect(admin.getByRole("status").filter({ hasText: `${email} was invited. They join when they open the link and accept.` })).toBeAttached();
  await admin.context().close();
});
