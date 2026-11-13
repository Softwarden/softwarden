import { expect, test, type Page } from "@playwright/test";
import { expectNoViolations, showTheme } from "./axe";
import { pageAs, post, token } from "./helpers";

/** Light and dark themes. The preference is System (the default, following the operating system),
 *  Light or Dark, chosen in the top bar and kept in this browser; index.html applies it before first paint. Also axe in
 *  both themes on the routes no other spec checks, so every route in the suite is covered in light and dark. */

// axe runs in both themes at each check (e2e/axe.ts).
test.describe.configure({ timeout: 180_000 });

const theme = (page: Page) => page.evaluate(() => document.documentElement.dataset.theme);

test("System follows the operating system; a chosen theme is kept, applied before the app loads, and System clears it", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Needs attention", level: 1 })).toBeVisible();
  expect(await theme(page)).toBe("dark");
  // System follows a change in the operating system without a reload.
  await page.emulateMedia({ colorScheme: "light" });
  await expect.poll(() => theme(page)).toBe("light");

  // The menu: a menu button with radio items; Escape returns focus to it.
  const button = page.getByRole("button", { name: "Theme: System" });
  await button.click();
  const menu = page.getByRole("menu", { name: "Theme" });
  await expect(menu.getByRole("menuitemradio", { name: "System" })).toHaveAttribute("aria-checked", "true");
  await expect(menu.getByRole("menuitemradio", { name: "System" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(button).toBeFocused();
  await button.click();
  await menu.getByRole("menuitemradio", { name: "Dark" }).click();
  expect(await theme(page)).toBe("dark");
  await expect(page.getByRole("button", { name: "Theme: Dark" })).toBeFocused();
  expect(await page.evaluate(() => localStorage.getItem("softwarden.theme"))).toBe("dark");

  // Dark wins over a light operating system, and is set by index.html before any app code runs (no flash): with the
  // app's script blocked, the attribute is still there.
  const bare = await page.context().newPage();
  await bare.emulateMedia({ colorScheme: "light" });
  await bare.route("**/src/main.tsx", (route) => route.abort());
  await bare.goto("/");
  expect(await theme(bare)).toBe("dark");
  await bare.close();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Needs attention", level: 1 })).toBeVisible();
  expect(await theme(page)).toBe("dark");

  // Light, then back to System, which forgets the stored choice.
  await page.getByRole("button", { name: "Theme: Dark" }).click();
  await page.getByRole("menu", { name: "Theme" }).getByRole("menuitemradio", { name: "Light" }).click();
  expect(await theme(page)).toBe("light");
  await page.getByRole("button", { name: "Theme: Light" }).click();
  await page.getByRole("menu", { name: "Theme" }).getByRole("menuitemradio", { name: "System" }).click();
  expect(await page.evaluate(() => localStorage.getItem("softwarden.theme"))).toBeNull();
  await page.emulateMedia({ colorScheme: "dark" });
  await expect.poll(() => theme(page)).toBe("dark");
});

test("Dark uses surface steps and the link blue; the sidebar stays ink", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "admin@example.test");
  const t = token();
  await post(page, "applications", { name: `${t} Ink`, lifecycle: "active" });
  await page.goto(`/applications?q=${t}`);
  await page.evaluate(() => localStorage.setItem("softwarden.theme", "dark"));
  await page.reload();
  await expect(page.getByRole("link", { name: `${t} Ink` })).toBeVisible();
  const colours = await page.evaluate(() => {
    const css = (el: Element | null | undefined, prop: string) => (el ? getComputedStyle(el).getPropertyValue(prop) : "");
    return {
      scheme: css(document.documentElement, "color-scheme"),
      canvas: css(document.body, "background-color"),
      card: css(document.querySelector("main [role=region]"), "background-color"),
      sidebar: css(document.querySelector("#primary-nav")?.closest(".bg-sidebar"), "background-color"), // the sidebar's container (it nests the nav)
      link: css(document.querySelector("main table a"), "color"),
    };
  });
  // tokens.json dark: background ink-950, card ink-900, sidebar ink-925 (as in light), link blue-300.
  expect(colours).toEqual({ scheme: "dark", canvas: "rgb(15, 19, 24)", card: "rgb(23, 28, 35)", sidebar: "rgb(20, 26, 36)", link: "rgb(143, 178, 248)" });
});

test("Routes no other spec checks with axe, in light and dark: component detail and edit, renewal edit, page not found, Viewer screens", async ({ browser, baseURL }) => {
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const t = token();
  const app = await post(admin, "applications", { name: `${t} Themed`, lifecycle: "deprecated", criticality: "critical" });
  const comp = await post(admin, "components", { name: `${t} Script`, lifecycle: "evaluating", component_type: "script", criticality: "high" });
  await post(admin, "relationships", { type: "depends_on", source: app.id, target: comp.id });
  const renewal = await post(admin, "renewals", { name: `${t} License`, auto_renew: "no", explicit_deadline: new Date(Date.now() + 20 * 86400000).toISOString().slice(0, 10) });

  await admin.goto(`/components/${comp.id}`);
  await expect(admin.getByRole("heading", { name: `${t} Script`, level: 1 })).toBeVisible();
  await expect(admin.getByText("Evaluating").first()).toBeVisible();
  await expect(admin.getByText("High criticality")).toBeVisible();
  await admin.waitForLoadState("networkidle");
  await expectNoViolations(admin, "Component detail");

  await admin.goto(`/components/${comp.id}/edit`);
  await expect(admin.getByRole("heading", { name: `Edit ${t} Script`, level: 1 })).toBeVisible();
  await expectNoViolations(admin, "Edit component");

  await admin.goto(`/renewals/${renewal.id}/edit`);
  await expect(admin.getByRole("heading", { name: `Edit ${t} License`, level: 1 })).toBeVisible();
  await expectNoViolations(admin, "Edit renewal");

  await admin.goto("/no-such-page");
  await expect(admin.getByRole("heading", { name: "Page not found", level: 1 })).toBeVisible();
  await expectNoViolations(admin, "Page not found");

  await admin.goto(`/applications?q=${t}`);
  await expect(admin.getByRole("link", { name: `${t} Themed` })).toBeVisible();
  await expect(admin.getByRole("row", { name: new RegExp(`${t} Themed`) })).toContainText("Deprecated");
  await expectNoViolations(admin, "Applications list with lifecycle chips");

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${app.id}`);
  await expect(viewer.getByRole("heading", { name: `${t} Themed`, level: 1 })).toBeVisible();
  await expect(viewer.getByText("Critical criticality")).toBeVisible();
  await viewer.waitForLoadState("networkidle");
  await expectNoViolations(viewer, "Application detail as a Viewer");
  await viewer.goto(`/applications/${app.id}/edit`);
  await expect(viewer.getByRole("heading", { name: "You can't change records", level: 1 })).toBeVisible();
  await expectNoViolations(viewer, "Edit as a Viewer");
  await viewer.goto("/admin/people");
  await expect(viewer.getByRole("heading", { name: "People & Teams", level: 1 })).toBeVisible();
  await viewer.waitForLoadState("networkidle");
  await expectNoViolations(viewer, "People as a Viewer");
});

test("Graph line art uses the line blue, at 3:1 on its surfaces in both themes (axe doesn't check SVG strokes)", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const target = await post(page, "applications", { name: `${t} Hub`, lifecycle: "active" });
  const other = await post(page, "applications", { name: `${t} Spoke`, lifecycle: "active" });
  await post(page, "relationships", { type: "depends_on", source: other.id, target: target.id });
  await post(page, "integrations", { name: `${t} Feed`, lifecycle: "active", source: target.id, target: other.id });
  await page.goto(`/dependencies?target=${target.id}&direction=both&depth=all&view=graph`);
  const graph = page.getByRole("application", { name: `Dependency graph for ${t} Hub` });
  await expect(graph).toBeVisible();
  const strokes = () => page.evaluate(() => {
    const stroke = (el: Element | null) => (el ? getComputedStyle(el).stroke : "missing");
    const svg = document.querySelector("svg[role=img]")!;
    return {
      flow: stroke(svg.querySelector("path[stroke-dasharray='7 5']")),
      arrow: stroke(svg.querySelector("#arrow-open path")),
      target: stroke(svg.querySelector("rect[stroke-width='3']")),
      pill: stroke(svg.querySelector("rect[stroke-dasharray='4 3']")),
      legend: stroke(document.querySelector("ul[aria-label=Legend] path[stroke-dasharray='7 5']")),
    };
  });
  // tokens.json accent-line: blue-600 in light, blue-300 in dark (tokens.mjs checks 3:1 on card, accent and popover).
  const blue = (rgb: string) => ({ flow: rgb, arrow: rgb, target: rgb, pill: rgb, legend: rgb });
  await showTheme(page, "light");
  expect(await strokes()).toEqual(blue("rgb(36, 87, 197)"));
  await showTheme(page, "dark");
  expect(await strokes()).toEqual(blue("rgb(143, 178, 248)"));
});
