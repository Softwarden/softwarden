import { createHmac } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { CLOUD, PLATFORM_EMAIL, TOTP_WEB } from "./cloud-server";
import { PASSWORD, post } from "./helpers";

/** The platform console's second factor end to end: against an installation with SOFTWARDEN_PLATFORM_TOTP=1 (cloud-server.ts,
 *  TOTP_WEB), platform staff get no console until they send a code. The first sign-in shows the setup key once; the
 *  spec reads it from the page and computes the codes as an authenticator app does (RFC 6238 below, Node's crypto),
 *  confirms it, impersonates with a fresh code (the one just used is refused), and a later sign-in asks for a code.
 *  Serial: the enrollment is the installation's (a fresh database every run). axe in light and dark. */

test.describe.configure({ mode: "serial", timeout: 180_000 });
test.skip(!CLOUD, "The second-factor e2e server runs only with E2E_CLOUD=1 (playwright.config.ts).");
test.use({ baseURL: TOTP_WEB });

// ---- RFC 6238 (TOTP over RFC 4226 HOTP): HMAC-SHA1, 6 digits, 30-second steps, as core/totp.py -------------------

function base32(secret: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of secret.replace(/[\s=]/g, "").toUpperCase()) bits += alphabet.indexOf(c).toString(2).padStart(5, "0");
  const bytes = bits.match(/.{8}/g) ?? [];
  return Buffer.from(bytes.map((b) => parseInt(b, 2)));
}
function codeAt(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", base32(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}
const stepNow = () => Math.floor(Date.now() / 30_000);

/** The server accepts a step within one of now, once, and only above the last one it accepted. */
let secret = "";
let lastStep = -1;
async function nextCode(): Promise<string> {
  // The next step above the last accepted; if that is more than one ahead of now, wait for the clock to catch up.
  while (lastStep + 1 > stepNow() + 1) await new Promise((done) => setTimeout(done, 1000));
  lastStep = Math.max(lastStep + 1, stepNow());
  return codeAt(secret, lastStep);
}

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(PLATFORM_EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

test("The first console sign-in sets up the second factor, and impersonating needs a fresh code", async ({ browser }) => {
  const page = await (await browser.newContext({ baseURL: TOTP_WEB })).newPage();
  await signIn(page);
  await expect(page.getByRole("heading", { level: 1, name: "Set up your second factor" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Platform" })).toHaveCount(0);
  // The API refuses every other console route until the code (403 second-factor-required).
  expect((await page.request.get("/api/v1/platform/organizations")).status()).toBe(403);
  await expectNoViolations(page, "Second factor setup (real)");

  await page.getByRole("button", { name: "Show the setup key" }).click();
  await expect(page.getByTestId("setup-key")).toContainText("If you leave before confirming, a new key replaces this one");
  secret = ((await page.getByTestId("setup-secret").textContent()) ?? "").replace(/\s+/g, "");
  expect(secret).toMatch(/^[A-Z2-7]{32}$/);
  await expect(page.getByTestId("setup-key").getByRole("link")).toHaveAttribute("href", new RegExp(`^otpauth://totp/.*secret=${secret}`));

  // A wrong code is a field error; the current one opens the console.
  await page.getByLabel("6-digit code (required)").fill(codeAt(secret, stepNow() + 5));
  await page.getByRole("button", { name: "Confirm and open the console" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "That code isn't right" })).toBeVisible();
  const signInCode = await nextCode();
  await page.getByLabel("6-digit code (required)").fill(signInCode);
  await page.getByRole("button", { name: "Confirm and open the console" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Organizations" })).toBeVisible();

  // Impersonate a member: the code used to sign in is refused as used; the next one starts it.
  await page.goto("/platform/organizations?q=contoso-synthetic");
  await page.getByRole("link", { name: "Contoso (synthetic)" }).click();
  await page.getByRole("region", { name: "Members table" }).getByRole("button", { name: "Impersonate Vic Viewer" }).click();
  const dialog = page.getByRole("dialog", { name: "Impersonate Vic Viewer" });
  await dialog.getByLabel("Reason (required)").fill("Second factor journey (synthetic).");
  await dialog.getByLabel("Your current password").fill(PASSWORD);
  await dialog.getByLabel("Code from your authenticator app (required)").fill(signInCode);
  await dialog.getByRole("button", { name: "Impersonate Vic Viewer" }).click();
  await expect(dialog.getByLabel("Code from your authenticator app (required)")).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByRole("alert").filter({ hasText: "already used" })).toBeVisible();
  await expectNoViolations(page, "Impersonate dialog with a code (real)");
  await dialog.getByLabel("Code from your authenticator app (required)").fill(await nextCode());
  await dialog.getByRole("button", { name: "Impersonate Vic Viewer" }).click();
  await expect(page.getByTestId("impersonation-banner")).toContainText("You are impersonating Vic Viewer");
  await post(page, "platform/impersonation", undefined, "DELETE");
  await page.context().close();
});

test("A later sign-in asks for the current code", async ({ browser }) => {
  test.skip(!secret, "Needs the enrollment of the test before.");
  const page = await (await browser.newContext({ baseURL: TOTP_WEB })).newPage();
  await signIn(page);
  await expect(page.getByRole("heading", { level: 1, name: "Enter your authenticator code" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Show the setup key" })).toHaveCount(0);
  await expectNoViolations(page, "Second factor code (real)");
  await page.getByLabel("6-digit code (required)").fill(await nextCode());
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Organizations" })).toBeVisible();
  await page.context().close();
});
