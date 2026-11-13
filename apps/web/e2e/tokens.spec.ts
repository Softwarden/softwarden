import { expectNoViolations } from "./axe";
import { expect, request, test } from "@playwright/test";
import { pageAs, token } from "./helpers";

/** An Admin creates a read-only API token, sees the secret once, uses it, and revokes it. */

test("An Admin creates a read-only token, uses it, and revokes it", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const name = `${token()} export`;
  await page.goto("/admin/tokens");
  await expect(page.getByRole("heading", { name: "API tokens" })).toBeVisible();
  await expectNoViolations(page, "API tokens page");

  await page.getByRole("button", { name: "Create token" }).first().click();
  const create = page.getByRole("dialog", { name: "Create API token" });
  await expect(create.getByLabel("Read-only")).toBeChecked();
  await expectNoViolations(page, "Create token dialog");
  await create.getByRole("button", { name: "Create token" }).click();
  await expect(create.getByRole("alert").filter({ hasText: "Fix these fields" })).toBeVisible();

  // The owner list shows names and roles; pick the seeded Viewer. (Not just the first "(Viewer)": admin.spec and
  // invitations.spec add invited Viewers, and a reset password leaves a Viewer who must change it before a token works.)
  const viewerOption = create.getByLabel(/^Owner/).locator("option", { hasText: "Vic Viewer (Viewer)" }).first();
  await create.getByLabel(/^Owner/).selectOption({ value: (await viewerOption.getAttribute("value")) ?? "" });
  await create.getByLabel(/^Name/).fill(name);
  await create.getByRole("button", { name: "Create token" }).click();

  const shown = page.getByRole("dialog", { name: "Copy your new token" });
  await expect(shown.getByText("It won't be shown again")).toBeVisible();
  const secret = (await shown.getByTestId("token-secret").textContent())!.trim();
  expect(secret).toMatch(/^sw_/);
  await expectNoViolations(page, "Secret dialog");
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(shown).toHaveCount(0);

  const row = page.getByRole("row").filter({ has: page.getByRole("rowheader", { name }) });
  await expect(row).toContainText("Read-only");
  await expect(row).toContainText("Active");
  await expect(page.getByText(secret)).toHaveCount(0); // never shown again

  // A client with no cookies, as a script would be: page.request carries the Admin's session, which would sign it in.
  const script = await request.newContext({ baseURL, extraHTTPHeaders: { Authorization: `Bearer ${secret}` } });
  expect((await script.get("/api/v1/applications")).status()).toBe(200);
  expect((await script.post("/api/v1/applications", { data: { name: `${name} app`, lifecycle: "active" } })).status()).toBe(403);

  await row.getByRole("button", { name: /^Revoke/ }).click();
  const revoke = page.getByRole("dialog", { name: `Revoke ${name}?` });
  await expectNoViolations(page, "Revoke dialog");
  await revoke.getByRole("button", { name: `Revoke ${name}` }).click();
  await expect(page.getByRole("status").filter({ hasText: `${name} was revoked.` })).toBeVisible();
  await expect(row).toContainText("Revoked");
  await expect(row.getByRole("button", { name: /^Revoke/ })).toHaveCount(0);

  expect((await script.get("/api/v1/applications")).status()).toBe(401);
  await script.dispose();
});

test("A reload asks for each list once: the query cache is cleared before the signed-in screens subscribe", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  await page.goto("/admin/tokens");
  await expect(page.getByRole("heading", { name: "API tokens", level: 1 })).toBeVisible();
  const asked: string[] = [];
  page.on("request", (r) => {
    const path = new URL(r.url()).pathname + new URL(r.url()).search;
    if (r.method() === "GET" && /^\/api\/v1\/(api-tokens|users|needs-attention\?scope=mine)$/.test(path)) asked.push(path);
  });
  await page.reload();
  await expect(page.getByRole("heading", { name: "API tokens", level: 1 })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(asked.sort()).toEqual(["/api/v1/api-tokens", "/api/v1/needs-attention?scope=mine", "/api/v1/users"]);
});
