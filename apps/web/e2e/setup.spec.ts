import { readFileSync } from "node:fs";
import { expectNoViolations } from "./axe";
import { expect, test, type Page } from "@playwright/test";
import { SETUP_TOKEN_FILE, SETUP_WEB } from "./setup-server";

/** First run: the setup page at /setup creates the first organization and Admin with the one-time token that
 *  `bootstrap_admin` prints. The journey runs against the clean installation in setup-server.ts; the error states the
 *  real server can't safely produce mid-run (rate limit, network) are answered by page.route on the main server. */

const ADMIN = { name: "Avery Quinn", email: "avery@first-run.example.test", password: "first-run-setup-2026" };

test.describe("first-run setup on a clean installation", () => {
  test.use({ baseURL: SETUP_WEB, timezoneId: "Europe/Berlin" });

  test("pending → bad token → field error → signed in → setup is done", async ({ page, browser }) => {
    // Signed out on a fresh install, any address lands on setup (by way of the sign-in page).
    await page.goto("/applications");
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.getByRole("heading", { name: "Set up Softwarden" })).toBeVisible();
    await expect(page.getByText("docker compose exec app python manage.py bootstrap_admin")).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Timezone" })).toHaveValue("Europe/Berlin"); // the browser's zone
    await expectNoViolations(page, "Setup form");

    // An empty submit names every field and focuses the first.
    await page.getByRole("button", { name: "Create organization and Admin" }).click();
    await expect(page.getByLabel("Setup token")).toBeFocused();
    await expect(page.getByLabel("Setup token")).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByText("Enter the organization name.").first()).toBeVisible();

    // A wrong token: refused, with the command to get the current one; the token field gets focus.
    await page.getByLabel("Setup token").fill("x".repeat(43));
    await page.getByLabel("Organization name").fill("Contoso First Run");
    await page.getByRole("combobox", { name: "Timezone" }).fill("New_York");
    await page.getByRole("option", { name: "America/New_York" }).click();
    await page.getByLabel("Your name").fill(ADMIN.name);
    await page.getByLabel("Your email").fill(ADMIN.email);
    await page.getByLabel("Password", { exact: true }).fill("password1234"); // 12 characters but common
    await page.getByLabel("Confirm password").fill("password1234");
    await page.getByRole("button", { name: "Create organization and Admin" }).click();
    const alert = page.getByRole("alert");
    await expect(alert).toContainText("This setup token is wrong or has expired.");
    await expect(alert).not.toContainText("`"); // page-owned text, not the server's Markdown
    await expect(alert).toContainText("docker compose exec app python manage.py bootstrap_admin");
    await expect(page.getByLabel("Setup token")).toBeFocused();
    await expectNoViolations(page, "Setup with a refused token");

    // The real token, pasted with surrounding whitespace, is trimmed. The server refuses the common password and
    // the token stays valid.
    const token = readFileSync(SETUP_TOKEN_FILE, "utf8").trim();
    await page.getByLabel("Setup token").fill(`  ${token}\n`);
    await expect(page.getByLabel("Setup token")).toHaveValue(token);
    await page.getByRole("button", { name: "Create organization and Admin" }).click();
    await expect(page.locator("#password-error")).toContainText("too common");
    await expect(page.getByLabel("Password", { exact: true })).toBeFocused();

    // Fixed: the Admin is created and signed in.
    await page.getByLabel("Password", { exact: true }).fill(ADMIN.password);
    await page.getByLabel("Confirm password").fill(ADMIN.password);
    await page.getByRole("button", { name: "Create organization and Admin" }).click();
    // The first Admin lands on the Dashboard, everyone's home.
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByRole("button", { name: /Avery Quinn · Admin/ })).toBeVisible(); // the sidebar's user menu
    const me = await (await page.request.get("/api/v1/auth/me")).json();
    expect(me.organization).toMatchObject({ name: "Contoso First Run", timezone: "America/New_York" });
    expect(me.role).toBe("admin");

    // Revisiting /setup: done, no form.
    await page.goto("/setup");
    await expect(page.getByRole("heading", { name: "Setup is done" })).toBeVisible();
    await expect(page.getByLabel("Setup token")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Go to Needs attention" })).toBeVisible();

    // Signed out, /setup points to sign-in and the sign-in page no longer redirects; the new Admin signs in.
    const signedOut = await browser.newContext({ baseURL: SETUP_WEB });
    const other = await signedOut.newPage();
    await other.goto("/setup");
    await expect(other.getByRole("heading", { name: "Setup is done" })).toBeVisible();
    await expectNoViolations(other, "Setup is done");
    await other.getByRole("link", { name: "Sign in" }).click();
    await expect(other).toHaveURL(/\/login$/);
    await other.getByLabel("Email").fill(ADMIN.email);
    await other.getByLabel("Password").fill(ADMIN.password);
    await other.getByRole("button", { name: "Sign in" }).click();
    await expect(other.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
    await expect(other).toHaveURL(/\/dashboard$/);
    await signedOut.close();
  });
});

test.describe("setup states the clean installation can't show mid-run", () => {
  /** Setup reports pending; each POST answers with the next of `replies`. */
  async function mockSetup(page: Page, replies: ((route: import("@playwright/test").Route) => Promise<void>)[]) {
    await page.route("**/api/v1/setup", async (route) => {
      if (route.request().method() === "GET") return route.fulfill({ json: { pending: true } });
      const reply = replies.shift();
      return reply ? reply(route) : route.abort();
    });
  }
  const problem = (status: number, body: Record<string, unknown>, headers: Record<string, string> = {}) =>
    (route: import("@playwright/test").Route) =>
      route.fulfill({ status, headers, contentType: "application/problem+json", body: JSON.stringify({ status, ...body }) });

  async function fillValid(page: Page) {
    await page.getByLabel("Setup token").fill("t".repeat(43));
    await page.getByLabel("Organization name").fill("Fabrikam");
    await page.getByLabel("Your name").fill("Robin Diaz");
    await page.getByLabel("Your email").fill("robin@example.test");
    await page.getByLabel("Password", { exact: true }).fill("a-long-enough-password");
    await page.getByLabel("Confirm password").fill("a-long-enough-password");
  }

  test("rate limited, network error, server field errors and already done", async ({ page }) => {
    await mockSetup(page, [
      problem(429, { detail: "Request was throttled. Expected available in 240 seconds." }, { "Retry-After": "240" }),
      (route) => route.abort("connectionrefused"),
      problem(400, { detail: "Invalid request", errors: { email: ["Enter an email address."], organization_name: ["Enter the organization name."] } }),
      problem(409, { detail: "Initial setup is already done." }),
    ]);
    await page.goto("/setup");
    await expect(page.getByRole("heading", { name: "Set up Softwarden" })).toBeVisible();
    await fillValid(page);
    const submit = page.getByRole("button", { name: "Create organization and Admin" });

    // Keyboard only: Enter in a field submits.
    await page.getByLabel("Confirm password").press("Enter");
    await expect(page.getByRole("alert")).toContainText("Too many wrong setup tokens from this address. Try again in 4 minutes.");
    await expect(page.getByRole("alert")).toBeFocused(); // no field to fix: the notice takes focus
    await expectNoViolations(page, "Setup rate limited");

    await submit.click();
    await expect(page.getByRole("alert")).toContainText("Couldn't reach the Softwarden server. The setup may not have finished");
    await expect(page.getByLabel("Organization name")).toHaveValue("Fabrikam"); // nothing typed is lost

    await submit.click();
    await expect(page.locator("#organization_name-error")).toHaveText("Enter the organization name.");
    await expect(page.locator("#email-error")).toHaveText("Enter an email address.");
    await expect(page.getByLabel("Organization name")).toBeFocused(); // first invalid field in form order

    await submit.click();
    await expect(page.getByRole("heading", { name: "Setup is done" })).toBeFocused();
    await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  });

  test("the setup state can't be loaded", async ({ page }) => {
    let fail = true;
    await page.route("**/api/v1/setup", (route) => (fail ? route.abort("connectionrefused") : route.fulfill({ json: { pending: true } })));
    await page.goto("/setup");
    await expect(page.getByRole("alert")).toContainText("Couldn't reach the Softwarden server.");
    await expectNoViolations(page, "Setup load error");
    fail = false;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByLabel("Setup token")).toBeVisible();
  });

  test("on the seeded server setup is closed and sign-in is not redirected", async ({ page }) => {
    await page.goto("/setup");
    await expect(page.getByRole("heading", { name: "Setup is done" })).toBeVisible();
    await page.goto("/login");
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });
});
