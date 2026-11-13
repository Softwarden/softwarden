import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { emailedInvitation } from "./helpers";

const PASSWORD = "softwarden-demo-2026"; // synthetic demo accounts from seed_demo

async function signIn(page: import("@playwright/test").Page, email: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("Admin invites a user by link; they join only when they open it and create their account", async ({ page, browser }) => {
  const email = `invited.${Date.now()}@example.test`;
  await signIn(page, "admin@example.test", PASSWORD);
  await page.getByRole("link", { name: "Users & roles" }).click();
  const opener = page.getByRole("button", { name: "Invite user" });
  await opener.click();
  const invite = page.getByRole("dialog", { name: "Invite user" });
  await invite.getByLabel("Email (required)").fill(email);
  await invite.getByLabel("Name").fill("Invited Person");
  await invite.getByLabel("Role").selectOption("viewer");
  await invite.getByRole("button", { name: "Invite" }).click();

  // The link, once: the e2e server can send email (locmem), so it was emailed too (`emailed`); the note takes
  // focus, and Escape or a click outside doesn't throw it away.
  const shown = page.getByRole("dialog", { name: "Invitation emailed" });
  await expect(shown.getByTestId("invite-note")).toBeFocused();
  await expect(shown.getByTestId("invite-note")).toContainText(`Emailed to ${email}.`);
  await expect(shown.getByTestId("invite-note")).toContainText("shown only once");
  await expect(shown.getByTestId("invite-summary")).toContainText(`${email} joins as Viewer when they open the link and accept.`);
  await expect(shown.getByTestId("invite-summary")).toContainText(/The link expires on \d{4}-\d{2}-\d{2} \d{2}:\d{2}\./);
  const link = (await shown.getByTestId("invite-link").textContent())!;
  expect(link).toMatch(/\/invite#[\w-]{20,}$/); // the token in the fragment, never in a path a server logs
  await expectNoViolations(page, "Invitation link dialog");
  await page.keyboard.press("Escape");
  await page.mouse.click(5, 5);
  await expect(shown).toBeVisible();
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(shown).toHaveCount(0);
  await expect(opener).toBeFocused();
  // Nobody is added until they accept.
  await expect(page.getByRole("rowheader", { name: /Alex Admin/ })).toBeVisible();
  await expect(page.getByRole("region", { name: "Users table" }).getByText(email)).toHaveCount(0);

  // The invitee opens the link from the invitation email in a browser of their own and chooses a name and password.
  // With more than one organization on the server only the emailed link creates an account; the one shown
  // here joins an existing account.
  const other = await (await browser.newContext()).newPage();
  await other.goto(new URL(await emailedInvitation(page, email), link).toString());
  await expect(other.getByRole("heading", { level: 1, name: "Join Contoso (synthetic)" })).toBeVisible();
  await expect(other).toHaveURL(/\/invite$/); // read, then taken out of the address bar
  await expect(other.getByText(/^i\*+@example\.test$/)).toBeVisible(); // the email, masked
  await expect(other.getByText("Viewer", { exact: true })).toBeVisible();
  await expect(other.getByLabel("Your name (required)")).toHaveValue("Invited Person");
  await expectNoViolations(other, "Invitation page, new account");
  await other.getByLabel("Password (required)", { exact: true }).fill("synthetic-new-passphrase");
  await other.getByLabel("Confirm password (required)").fill("synthetic-other-passphrase");
  await other.getByRole("button", { name: "Create account and join Contoso (synthetic)" }).click();
  await expect(other.getByText("The passwords don't match.", { exact: true }).last()).toBeVisible();
  await other.getByLabel("Confirm password (required)").fill("synthetic-new-passphrase");
  await other.getByRole("button", { name: "Create account and join Contoso (synthetic)" }).click();
  await expect(other.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible(); // everyone's home
  await expect(other).toHaveURL(/\/dashboard$/);
  await expect(other.getByRole("link", { name: "Users & roles" })).toHaveCount(0); // Viewer
  // The link is used: opening it again gets the one answer for a link that can't be used.
  await other.goto(link);
  await expect(other.getByRole("heading", { level: 1, name: "This invitation link can't be used" })).toBeVisible();
  await other.context().close();

  // The Admin now sees the new member.
  await page.reload();
  await expect(page.getByRole("rowheader").filter({ hasText: email })).toContainText("Invited Person");
});

test("Editor adds a team, which can be deleted while unused", async ({ page }) => {
  const team = `Team ${Date.now()}`;
  await signIn(page, "editor@example.test", PASSWORD);
  await page.getByRole("link", { name: "People & Teams" }).click();
  await page.getByRole("tab", { name: "Teams" }).click();
  await page.getByRole("button", { name: "Add team" }).click();
  await page.getByRole("dialog", { name: "Add team" }).getByLabel("Name (required)").fill(team);
  await page.getByRole("dialog", { name: "Add team" }).getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("rowheader", { name: team })).toBeVisible();

  // Unused, so it can be deleted; in-use teams and refusals are covered in people.spec.ts.
  await expect(page.getByRole("button", { name: `Delete ${team}` })).toBeVisible();
});
