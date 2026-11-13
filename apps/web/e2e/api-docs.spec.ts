import { expectNoViolations } from "./axe";
import { expect, request, test, type Page } from "@playwright/test";
import { pageAs, token } from "./helpers";

/** The API reference (this installation's OpenAPI schema drawn by Scalar, lazily loaded, nothing sent to a
 *  third party) and the API guide (docs/API.md in the app), which every role may open; the API token copy-once flow
 *  (the secret appears once, a reload shows only its prefix, revoke works); and what Editors and Viewers see. */

/** Every request the page makes to a host other than this installation's. */
function outsideRequests(page: Page, baseURL: string | undefined) {
  const own = new URL(baseURL ?? "http://localhost").host;
  const outside: string[] = [];
  page.on("request", (r) => {
    const url = new URL(r.url());
    if (!["http:", "https:", "ws:", "wss:"].includes(url.protocol)) return;
    if (url.host !== own && !["localhost", "127.0.0.1"].includes(url.hostname)) outside.push(r.url());
  });
  return outside;
}

test("The API reference renders the schema with tokens' colors, and nothing leaves the installation", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  // Count zod's eval probe (`new Function("")`), which the installation's CSP reports as a violation: Scalar's zod runs
  // jitless (src/features/api-docs/zod-jitless.ts), so there must be none.
  await page.addInitScript(() => {
    const probes = { count: 0 };
    (window as unknown as { __evalProbes: typeof probes }).__evalProbes = probes;
    window.Function = new Proxy(Function, {
      construct(target, args) {
        if (args.length === 1 && args[0] === "") probes.count++;
        return Reflect.construct(target, args);
      },
    });
  });
  const outside = outsideRequests(page, baseURL);
  const schema: string[] = [];
  page.on("request", (r) => { if (new URL(r.url()).pathname === "/api/v1/schema") schema.push(r.url()); });
  await page.goto("/admin/api");
  await expect(page.getByRole("heading", { level: 1, name: "API reference" })).toBeVisible();
  const reference = page.getByTestId("api-reference");
  // Scalar draws the schema the page fetched: its title and description. Its title is an h1 Scalar won't let us
  // change: it is hidden (the page's own h1 names the page) and still demoted to level 2.
  const title = reference.locator("h1", { hasText: "Softwarden API" });
  await expect(title).toBeAttached({ timeout: 30_000 });
  await expect(title).toBeHidden();
  await expect(title).toHaveAttribute("aria-level", "2");
  await expect(page.locator("h1:visible")).toHaveCount(1);
  await expect(reference).toContainText("keyset-paginated");
  expect(await page.evaluate(() => (window as unknown as { __evalProbes: { count: number } }).__evalProbes.count)).toBe(0);
  expect(await page.evaluate(() => (globalThis as unknown as { __zod_globalConfig?: { jitless?: boolean } }).__zod_globalConfig?.jitless)).toBe(true);
  // The counter itself works: a probe made here is counted.
  expect(await page.evaluate(() => { new Function(""); return (window as unknown as { __evalProbes: { count: number } }).__evalProbes.count; })).toBe(1);
  // One level-1 heading in the accessibility tree (Playwright's role engine reads h1's native level, not aria-level).
  await expect(page.locator("main h1:not([aria-level='2'])")).toHaveCount(1);
  // And every operation: the webhooks group lists its routes by summary.
  await reference.getByText("webhooks", { exact: true }).first().click();
  await expect(reference.getByText("Webhook endpoints (Admin)").first()).toBeVisible();
  expect(schema.length).toBeGreaterThanOrEqual(1);
  // Scalar's colours are the tokens, in both themes: inside its root, its background and text variables compute to the
  // card and foreground tokens of the theme shown.
  for (const theme of ["light", "dark"] as const) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    const colours = await page.evaluate(() => {
      const root = document.querySelector<HTMLElement>("[data-testid=api-reference] .scalar-app") ?? document.querySelector<HTMLElement>("[data-testid=api-reference]")!;
      const probe = (css: string) => {
        const el = document.createElement("div");
        el.style.backgroundColor = css;
        root.append(el);
        const value = getComputedStyle(el).backgroundColor;
        el.remove();
        return value;
      };
      return {
        background: [probe("var(--scalar-background-1)"), probe("var(--card)")],
        text: [probe("var(--scalar-color-1)"), probe("var(--foreground)")],
        link: [probe("var(--scalar-color-accent)"), probe("var(--link)")],
      };
    });
    for (const [scalar, own] of Object.values(colours)) expect(scalar).toBe(own);
  }
  await expectNoViolations(page, "API reference");

  // The guide's topics, from the reference page.
  await page.getByRole("link", { name: "Rate limits" }).click();
  await expect(page).toHaveURL(/\/admin\/api\/guide#rate-limits$/);
  await expect(page.getByRole("heading", { level: 1, name: "API guide" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Rate limits" })).toBeFocused();
  await expect(page.getByTestId("api-guide")).toContainText("Retry-After");
  await expect(page.getByTestId("api-guide").getByRole("heading", { name: "Verifying a webhook" })).toBeVisible();
  await expectNoViolations(page, "API guide");
  expect(outside, "requests to other hosts").toEqual([]);
});

test("Every role may read the API reference; only Admins open API tokens and webhooks", async ({ browser, baseURL }) => {
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/");
  await viewer.getByRole("button", { name: /Account$/ }).click();
  await viewer.getByRole("menuitem", { name: "API reference" }).click();
  await expect(viewer.getByRole("heading", { level: 1, name: "API reference" })).toBeVisible();
  await expect(viewer.getByText("Ask an Admin for an API token")).toBeVisible();
  await expect(viewer.getByTestId("api-reference")).toBeVisible({ timeout: 30_000 });
  // The Admin section stays Admin pages only, but the command palette offers the reference to every role.
  await expect(viewer.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "API reference" })).toHaveCount(0);
  await viewer.keyboard.press("Control+k");
  const palette = viewer.getByRole("dialog", { name: "Command palette" });
  await palette.getByRole("combobox", { name: "Search records, pages and actions" }).fill("API reference");
  await expect(palette.getByRole("option", { name: /API reference/ })).toBeVisible();
  await viewer.keyboard.press("Escape");

  await viewer.goto("/admin/tokens");
  await expect(viewer.getByTestId("admin-only")).toContainText("Only Admins can open API tokens.");
  await expectNoViolations(viewer, "API tokens for a Viewer");

  const editor = await pageAs(browser, baseURL, "editor@example.test");
  for (const [path, title] of [["/admin/webhooks", "Webhooks"], ["/admin/webhooks/deliveries", "Webhook delivery log"], ["/admin/tokens", "API tokens"]]) {
    await editor.goto(path);
    await expect(editor.getByTestId("admin-only")).toContainText(`Only Admins can open ${title}.`);
  }
  await expect(editor.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Webhooks" })).toHaveCount(0);
  await expectNoViolations(editor, "Webhook delivery log for an Editor");
  await viewer.context().close();
  await editor.context().close();
});

test("A new token is shown once: after a reload only its prefix is, and revoking it stops it", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const name = `${token()} copy once`;
  await page.goto("/admin/tokens");
  await expect(page.getByRole("heading", { level: 1, name: "API tokens" })).toBeVisible();
  await page.getByRole("button", { name: "Create token" }).first().click();
  const create = page.getByRole("dialog", { name: "Create API token" });
  const viewerOption = create.getByLabel(/^Owner/).locator("option", { hasText: "Vic Viewer (Viewer)" }).first();
  await create.getByLabel(/^Owner/).selectOption({ value: (await viewerOption.getAttribute("value")) ?? "" });
  await create.getByLabel(/^Name/).fill(name);
  await create.getByRole("button", { name: "Create token" }).click();

  const shown = page.getByRole("dialog", { name: "Copy your new token" });
  await expect(shown.getByRole("note")).toContainText("Shown only once.");
  const secret = (await shown.getByTestId("token-secret").textContent())!.trim();
  expect(secret).toMatch(/^sw_[\w-]{40}$/);
  await expectNoViolations(page, "New token, shown once");
  // Focus starts on the warning, and neither a stray click outside nor Escape throws the only copy away.
  await expect(shown.getByTestId("secret-note")).toBeFocused();
  // Focused by script after a mouse click, the note still shows its focus ring (focus, not focus-visible).
  expect(await shown.getByTestId("secret-note").evaluate((el) => getComputedStyle(el).boxShadow)).not.toBe("none");
  await page.mouse.click(5, 5);
  await expect(shown).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(shown).toBeVisible();
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await shown.getByRole("button", { name: "Copy" }).click();
  await expect(shown.getByRole("status")).toHaveText("Token copied to the clipboard.");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(secret);
  await shown.getByRole("button", { name: "Done" }).click();
  await expect(shown).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  // Focus is back on the button that opened it, or on the page heading when that was the empty state's button (gone
  // now that the list has a token).
  await expect.poll(() => page.evaluate(() => {
    const el = document.activeElement;
    return el?.tagName === "H1" ? "heading" : el?.textContent?.trim() ?? "";
  })).toMatch(/^(heading|Create token)$/);
  const prefix = secret.slice(0, 11);
  await expect(page.getByRole("status").filter({ hasText: `${name} was created. Only its prefix, ${prefix}…, is shown from now on.` })).toBeAttached();

  const row = page.getByRole("row").filter({ has: page.getByRole("rowheader", { name }) });
  await expect(row.getByTestId("token-prefix")).toHaveText(`${prefix}…`);
  await expect(page.getByText(secret)).toHaveCount(0);
  await page.reload();
  await expect(row.getByTestId("token-prefix")).toHaveText(`${prefix}…`);
  expect(await page.content()).not.toContain(secret);
  // The API never returns it again either.
  const listed = await (await page.request.get("/api/v1/api-tokens")).text();
  expect(listed).not.toContain(secret);
  await expect(row).toContainText("Active");

  const script = await request.newContext({ baseURL, extraHTTPHeaders: { Authorization: `Bearer ${secret}` } });
  expect((await script.get("/api/v1/applications")).status()).toBe(200);
  // Tokens are managed from a session only: a token can't list them, even a read.
  expect((await script.get("/api/v1/api-tokens")).status()).toBe(403);

  await row.getByRole("button", { name: /^Revoke/ }).click();
  await page.getByRole("dialog", { name: `Revoke ${name}?` }).getByRole("button", { name: `Revoke ${name}` }).click();
  await expect(row).toContainText("Revoked");
  expect((await script.get("/api/v1/applications")).status()).toBe(401);
  await script.dispose();
  await expectNoViolations(page, "API tokens after a revoke");
});
