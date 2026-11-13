import { expect, type Locator, type Page, test } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** WCAG 1.4.10: a dialog taller than the viewport scrolls inside itself, so its title,
 *  its Close button and its footer buttons stay reachable at 640x360 (a landscape phone) and 320x256 (1280x1024 at
 *  400% zoom). Every dialog reachable from /admin/tokens, /applications and /tags is opened at both sizes. */

test.describe.configure({ timeout: 180_000 });

const SIZES = [{ width: 640, height: 360 }, { width: 320, height: 256 }];

/** The open dialog's title starts on screen, the dialog ends on screen, and every button in it (Close included) can be
 *  scrolled into view inside the dialog. */
async function expectFits(page: Page, dialog: Locator, what: string) {
  await expect(dialog, what).toBeVisible();
  // The open animation moves the dialog; measure where it settles.
  await page.evaluate(async () => {
    const frame = () => new Promise((done) => requestAnimationFrame(done));
    for (let i = 0; i < 100 && document.getAnimations().some((a) => a.playState === "running" || a.pending); i++) await frame();
  });
  const vh = page.viewportSize()!.height;
  const title = await dialog.locator('[data-slot="dialog-title"]').first().boundingBox();
  expect(title, `${what}: a title`).not.toBeNull();
  expect(title!.y, `${what}: the title's top is on screen`).toBeGreaterThanOrEqual(0);
  const box = (await dialog.boundingBox())!;
  expect(box.y, `${what}: the dialog's top is on screen`).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height, `${what}: the dialog's bottom is on screen`).toBeLessThanOrEqual(vh + 0.5);
  for (const button of await dialog.getByRole("button").all()) {
    if (!(await button.isVisible())) continue;
    const name = (await button.textContent())?.trim() || (await button.getAttribute("aria-label")) || "button";
    await button.scrollIntoViewIfNeeded();
    const b = (await button.boundingBox())!;
    expect(b.y, `${what}: "${name}" can be scrolled on screen (top)`).toBeGreaterThanOrEqual(0);
    expect(b.y + b.height, `${what}: "${name}" can be scrolled on screen (bottom)`).toBeLessThanOrEqual(vh + 0.5);
  }
  await expect(dialog.getByRole("button", { name: "Close" }).first(), `${what}: a Close button`).toBeAttached();
}

async function closeDialog(page: Page) {
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

test("Every dialog on API tokens, Applications and Tags fits a short or zoomed screen", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const t = token();
  const me = await (await page.request.get("/api/v1/auth/me")).json();
  const day = new Date(Date.now() + 30 * 86_400_000).toISOString().slice(0, 10);
  await post(page, "api-tokens", { user: me.user.id, name: `${t} token`, read_only: true, expires_on: day });
  await post(page, "applications", { name: `${t} Fit`, lifecycle: "active", tags: [`${t}-fit`] });

  for (const size of SIZES) {
    await page.setViewportSize(size);
    const at = `${size.width}x${size.height}`;

    await page.goto("/admin/tokens");
    await page.getByRole("button", { name: "Create token" }).first().click();
    await expectFits(page, page.getByRole("dialog", { name: "Create API token" }), `${at} Create API token`);
    await closeDialog(page);
    await page.getByRole("button", { name: `Revoke ${t} token` }).click();
    await expectFits(page, page.getByRole("dialog", { name: `Revoke ${t} token?` }), `${at} Revoke token`);
    await closeDialog(page);

    await page.goto(`/applications?q=${t}`);
    await expect(page.getByRole("checkbox", { name: `Select ${t} Fit` })).toBeVisible();
    await page.getByRole("button", { name: "Save as view…" }).click();
    await expectFits(page, page.getByRole("dialog", { name: "Save as view" }), `${at} Save as view`);
    await closeDialog(page);
    for (const format of ["CSV", "Excel"]) {
      await page.getByRole("button", { name: `Export ${format}…` }).click();
      await expectFits(page, page.getByRole("dialog"), `${at} Export ${format}`);
      await closeDialog(page);
    }
    await page.getByRole("checkbox", { name: `Select ${t} Fit` }).check();
    for (const action of ["Bulk edit…", "Bulk delete…"]) {
      await page.getByRole("region", { name: "Bulk actions" }).getByRole("button", { name: action }).click();
      await expectFits(page, page.getByRole("dialog"), `${at} ${action}`);
      await closeDialog(page);
    }

    await page.goto("/tags");
    await page.getByRole("button", { name: `Edit tag ${t}-fit` }).click();
    await expectFits(page, page.getByRole("dialog"), `${at} Edit tag`);
    await closeDialog(page);
  }
  await page.context().close();
});

/** What is drawn at the center of `el` belongs to `el` (it is on screen and nothing covers it). */
async function onTop(page: Page, el: Locator) {
  const b = (await el.boundingBox())!;
  const handle = await el.elementHandle();
  return page.evaluate(({ x, y, target }) => { const at = document.elementFromPoint(x, y); return !!at && !!target && (target === at || target.contains(at)); },
    { x: b.x + b.width / 2, y: b.y + b.height / 2, target: handle });
}

/** Inside the dialog's scroll box a record picker's list is a fixed layer, never clipped at the dialog's edge,
 *  and the title and Close stay in view while the dialog scrolls. */
test("A picker's options in Add relationship are all reachable, and the header stays in view", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const portal = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  for (let i = 0; i < 10; i++) await post(page, "applications", { name: `${t} Peer ${String(i).padStart(2, "0")}`, lifecycle: "active" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/applications/${portal.id}?tab=relationships`);
  await page.getByRole("button", { name: "Add relationship" }).click();
  const dialog = page.getByRole("dialog", { name: `Add relationship — ${t} Portal` });
  await dialog.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} Peer`);
  const options = page.getByRole("listbox").getByRole("option", { name: new RegExp(`${t} Peer`) });
  // The picker lists the first 8 matches of the 10 (the record search's page); the list is taller than the room below.
  await expect(options).toHaveCount(8);
  const last = options.last();
  const name = (await last.textContent())?.match(new RegExp(`${t} Peer \\d\\d`))?.[0] ?? "";
  expect(name).not.toBe("");
  // Scroll the list itself, not the dialog: the dialog stays where it was, and the list's last option must show.
  await page.getByRole("listbox").evaluate((el) => { el.scrollTop = el.scrollHeight; });
  expect(await dialog.evaluate((el) => el.scrollTop), "the dialog didn't scroll").toBe(0);
  await expect(last).toBeInViewport();
  expect(await onTop(page, last), "the last option is not clipped or covered").toBe(true);
  await last.click();
  await expect(dialog.getByRole("combobox", { name: "Other record (required)" })).toHaveValue(new RegExp(`^${name}`));

  // A short screen makes the same dialog scroll: scrolled to the bottom, its title and Close are still on screen.
  await page.setViewportSize({ width: 640, height: 360 });
  await dialog.evaluate((el) => { el.scrollTop = el.scrollHeight; });
  expect(await dialog.evaluate((el) => el.scrollTop), "the dialog scrolls at 640x360").toBeGreaterThan(0);
  const title = dialog.locator('[data-slot="dialog-title"]');
  const close = dialog.getByRole("button", { name: "Close" });
  await expect(title).toBeInViewport();
  await expect(close).toBeInViewport();
  expect(await onTop(page, title), "title in view").toBe(true);
  expect(await onTop(page, close), "Close in view").toBe(true);
  await page.context().close();
});
