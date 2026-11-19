import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";
import { assertSafePath } from "../src/lib/safe-path";

/** An id in the address bar that isn't a UUID never reaches an API path. A crafted link such as
 *  /license-pools/..%2Fvendors%2F<id> shows the not-found page and makes no request. */

test.describe.configure({ timeout: 120_000 });

test("A link whose id climbs out of its resource shows not found and calls no other API path", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const vendor = await post(page, "vendors", { name: `${token()} Northwind` });
  const requests: string[] = [];
  page.on("request", (r) => { if (r.url().includes("/api/")) requests.push(decodeURIComponent(r.url())); });

  const crafted = [
    `/license-pools/..%2Fvendors%2F${vendor.id}`, `/license-pools/..%2Fvendors%2F${vendor.id}/edit`, `/contracts/..%2Fvendors%2F${vendor.id}`,
    `/renewals/%2e%2e%2Fvendors%2F${vendor.id}`, `/applications/..%5Cvendors%5C${vendor.id}`, `/admin/audit/..%2Fvendors%2F${vendor.id}`,
    "/vendors/not-a-uuid", `/discovery/runs/..%2Fconnectors/${vendor.id}`,
  ];
  for (const route of crafted) {
    await page.goto(route);
    await expect(page.getByRole("heading", { level: 1 }), route).toHaveText(/not found/i);
    await expect(page.getByRole("button", { name: /retry/i })).toHaveCount(0);
    await expect(page.getByRole("main").getByRole("link", { name: /^Back to / })).toBeVisible();
  }
  // The shell's own calls are fine; none may name a record path or climb out of /api/v1/.
  expect(requests.filter((u) => /\/api\/v1\/(vendors|license-pools|contracts|renewals|applications|audit-events|discovery\/runs|connectors|discovery-runs|people|imports|notification)|\.\./.test(u))).toEqual([]);

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`/license-pools/..%2Fvendors%2F${vendor.id}`);
  await expectNoViolations(page, "A crafted id: not found (light)");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await expectNoViolations(page, "A crafted id: not found (dark)");

  // A real id still opens.
  await page.goto(`/vendors/${vendor.id}`);
  await expect(page.getByRole("heading", { level: 1 })).not.toHaveText(/not found/i);
  await page.context().close();
});

test("A search-parameter id that smuggles a dot segment through a tab is ignored, not requested", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const vendor = await post(page, "vendors", { name: `${token()} Northwind` });
  const requests: string[] = [];
  page.on("request", (r) => { if (r.url().includes("/api/")) requests.push(decodeURIComponent(r.url())); });
  const smuggled = `.%09.%2Fvendors%2F${vendor.id}`;
  for (const route of [`/discovery/setup/entra?connector=${smuggled}`, `/discovery/setup/entra?connector=${vendor.id}&step=finish&run=${smuggled}`, `/assessments/${vendor.id}?snapshot=${smuggled}`]) {
    await page.goto(route);
    await page.waitForLoadState("networkidle");
  }
  expect(requests.filter((u) => /\/api\/v1\/vendors\/|\.\.|\.[\t]./.test(u))).toEqual([]);
  await page.context().close();
});

test("The request choke point refuses a path that climbs out of /api/v1/", () => {
  for (const bad of ["x/../y", "vendors/..%2Fusers", "vendors/%2E%2E/x", "a\\b", "a%5Cb", "a%2fb", "x/.\t./y", "x/.\n./y", "x/.\r./y", "x/a b", "x/.\t./y#z", "license-pools/..%2Fvendors%2Fid?x=1"]) {
    expect(() => assertSafePath(bad), bad).toThrow();
  }
  for (const ok of ["vendors", "vendors/00000000-0000-4000-8000-000000000000", "applications/?q=a..b&next=%2Fx", "audit-events?scope=a%2Fb"]) {
    expect(() => assertSafePath(ok), ok).not.toThrow();
  }
});
