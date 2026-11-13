import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs } from "./helpers";

/** On the main (community) e2e server: limits are unlimited there, every role can read them, and the sign-in
 *  page offers no signup (SOFTWARDEN_SIGNUP_ENABLED is off). The cloud journeys are in cloud.spec.ts. */

test("A Viewer reads Usage and limits from the account menu: every limit is Unlimited on the community edition", async ({ browser, baseURL }) => {
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/dashboard");
  await viewer.getByRole("button", { name: /\. Account$/ }).click();
  await expect(viewer.getByRole("menuitem", { name: "Platform console" })).toHaveCount(0); // not platform staff
  await viewer.getByRole("menuitem", { name: "Usage and limits" }).click();
  await expect(viewer.getByRole("heading", { level: 1, name: "Usage and limits" })).toBeVisible();
  await expect(viewer.getByTestId("limit-records")).toContainText(/records used · Unlimited/);
  await expect(viewer.getByTestId("limit-api_rate_write")).toContainText("Unlimited");
  await expect(viewer.getByTestId("limit-records").getByText("Limit reached")).toHaveCount(0);
  await expectNoViolations(viewer, "Usage and limits, community edition");
  // The console is staff only: anyone else is sent home.
  await viewer.goto("/platform/organizations");
  await expect(viewer.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  await viewer.context().close();
});

test("Every limit has its short name, and a refused invitation names the send budget and what was sent in words", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  await admin.goto("/admin/limits");
  await expect(admin.getByRole("heading", { level: 1, name: "Usage and limits" })).toBeVisible();
  // The send budgets and the catch-all for secondary tables read as names, not as the API's long label.
  await expect(admin.getByTestId("limit-invitation_emails_hour").getByRole("heading", { level: 3 })).toHaveText("Invitation emails per hour");
  await expect(admin.getByTestId("limit-invitation_emails_day").getByRole("heading", { level: 3 })).toHaveText("Invitation emails per day");
  await expect(admin.getByTestId("limit-invitation_emails_hour")).toContainText(/emails sent in the last hour · Unlimited/);
  await expect(admin.getByTestId("limit-other_records").getByRole("heading", { level: 3 })).toHaveText("Other records");
  await expect(admin.getByTestId("limit-other_records")).toContainText("People, vendors, contracts, relationships, tags");
  await expectNoViolations(admin, "Usage and limits with the send budgets");

  // A budget the API refuses an invitation for (the community edition has none, so the refusal is the cloud's, sent
  // here as the API words it): the dialog says which limit, how many were sent in its window, then the API's advice.
  await admin.route("**/api/v1/invitations", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fulfill({
      status: 409, contentType: "application/problem+json",
      json: {
        type: "/problems/limit-reached", title: "Limit reached", status: 409, limit: "invitation_emails_hour", maximum: 5, usage: 5,
        detail: "This organization has sent its 5 invitation emails for this hour. Try again later, set up your organization's own mail server in Email settings, or ask the service operator to raise the limit.",
      },
    });
  });
  await admin.goto("/admin/users");
  await admin.getByRole("button", { name: "Invite user" }).click();
  const invite = admin.getByRole("dialog", { name: "Invite user" });
  await invite.getByLabel("Email (required)").fill(`budget.${Date.now()}@example.test`);
  await invite.getByRole("button", { name: "Invite" }).click();
  await expect(invite).toContainText("Limit reached: Invitation emails per hour, 5 of 5 sent in the last hour. This organization has sent its 5 invitation emails for this hour.");
  await expectNoViolations(admin, "Invitation refused by the send budget");
  await admin.context().close();
});

test("Signup is off on the main server: no Create an account link, and /signup says so", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in to Softwarden" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Create an account" })).toHaveCount(0);
  await page.goto("/signup");
  await expect(page.getByRole("heading", { level: 1, name: "Signup is turned off" })).toBeVisible();
  await expectNoViolations(page, "Signup turned off");
});
