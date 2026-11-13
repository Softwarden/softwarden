import { expectNoViolations } from "./axe";
import { expect, test, type Browser, type Page } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** Administration › Webhooks and the webhook delivery log, typed from the generated OpenAPI client. Add an
 *  endpoint with events picked by family, ping it (posts to `*.e2e.test` are answered by the e2e server: 202, 500 for
 *  `fail.`, 410 Gone for `gone.`; api/e2e_views.py), read the delivery with its attempts and payload, resend it, edit
 *  the events, rotate the secret with a grace period, filter the log in the URL, and see an endpoint Softwarden turned
 *  off (the Admin banner) turned back on. Endpoints are organization-wide, so the spec has its own organization
 *  (`e2e/spec-org`, `webhooks`); its tests run in order. `e2e/run-digest` with `webhooks: true` is the worker's tick. */

const ADMIN = "wh-admin@example.test";

test.describe.configure({ mode: "serial" });

let made: Promise<void> | null = null;
async function as(browser: Browser, baseURL: string | undefined, email: string) {
  made ??= (async () => {
    const contoso = await pageAs(browser, baseURL, "admin@example.test");
    await post(contoso, "e2e/spec-org", { which: "webhooks" });
    await contoso.context().close();
  })();
  await made;
  return pageAs(browser, baseURL, email);
}

const t = token();
const HOOK = `${t} Ticketing`;
const BROKEN = `${t} Broken receiver`;
const GONE = `${t} Retired receiver`;
const URL_SECRET = "URL-TOKEN-0123456789";
const SIGNING = "wh-signing-secret-0123456789";
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const rowOf = (page: Page, name: string) => page.getByRole("row").filter({ has: page.getByRole("rowheader", { name: new RegExp(`^${escape(name)}`) }) });
const said = (page: Page, text: string) => expect(page.getByRole("status").filter({ hasText: text }).first()).toBeAttached();
const tick = (page: Page) => post(page, "e2e/run-digest", { webhooks: true });

test("Add a webhook with events by family, ping it, read the delivery and resend it", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/admin/webhooks");
  await expect(page.getByRole("heading", { level: 1, name: "Webhooks" })).toBeVisible();
  await expect(page.getByText("No webhooks yet")).toBeVisible();
  await expectNoViolations(page, "Webhooks, empty");

  await page.getByRole("button", { name: "Add webhook" }).click();
  const dialog = page.getByRole("dialog", { name: "Add webhook" });
  await dialog.getByRole("button", { name: "Add webhook" }).click();
  await expect(dialog.getByRole("alert").filter({ hasText: "Fix these fields" })).toBeVisible();

  await dialog.getByLabel("Name (required)").fill(HOOK);
  await dialog.getByLabel("Description").fill("The service desk's intake");
  await dialog.getByLabel("Address (required)").fill(`https://hooks.e2e.test/softwarden?token=${URL_SECRET}`);
  await dialog.getByRole("button", { name: "Generate a secret" }).click();
  const secretBox = dialog.getByLabel("Signing secret (required)");
  await expect(secretBox).toHaveAttribute("type", "text");
  const generated = await secretBox.inputValue();
  expect(generated).toMatch(/^[\w-]{32}$/);

  const records = dialog.getByRole("group", { name: "Records" });
  await records.getByLabel(/^Every records event/).check();
  // A type its family's wildcard covers is shown as included.
  await expect(records.getByLabel(/^record\.updated/)).toBeChecked();
  await expect(records.getByLabel(/^record\.updated/)).toBeDisabled();
  await dialog.getByRole("group", { name: "Renewals" }).getByLabel(/^renewal\.decided/).check();
  await expect(dialog.getByText("Chosen: record.*, renewal.decided.")).toBeVisible();
  await expectNoViolations(page, "Add webhook dialog");
  await dialog.getByRole("button", { name: "Add webhook" }).click();
  await expect(dialog).toHaveCount(0);
  await said(page, `Webhook ${HOOK} added. Ping it to check that it answers.`);

  const row = rowOf(page, HOOK);
  await expect(row).toContainText("hooks.e2e.test");
  await expect(row).toContainText("record.*, renewal.decided");
  await expect(row).toContainText("Nothing sent yet");
  await expect(row).toContainText("On");
  // The address and the secret are write-only: never on the page, never in the API's answer.
  await expect(page.locator("body")).not.toContainText(URL_SECRET);
  const listed = await (await page.request.get("/api/v1/webhooks")).text();
  expect(listed).not.toContain(URL_SECRET);
  expect(listed).not.toContain(generated);
  await expectNoViolations(page, "Webhooks with an endpoint");

  await row.getByRole("button", { name: `Ping ${HOOK}` }).click();
  await expect(page.getByTestId("ping-result")).toContainText(`Ping to ${HOOK}: the endpoint answered HTTP 202`);
  await expect(row).toContainText("Sent");
  await expect(row).toContainText("HTTP 202");

  await page.getByTestId("ping-result").getByRole("link", { name: /View the ping in the delivery log/ }).click();
  await expect(page.getByRole("heading", { level: 1, name: `ping to ${HOOK}` })).toBeVisible();
  const attempts = page.getByRole("region", { name: "Attempts table" });
  await expect(attempts.getByRole("row").nth(1)).toContainText("Sent");
  await expect(attempts.getByRole("row").nth(1)).toContainText("HTTP 202");
  const payload = page.getByTestId("webhook-payload");
  await expect(payload).toContainText('"type": "ping"');
  await expect(payload).toContainText(HOOK);
  await expect(page.locator("body")).not.toContainText(URL_SECRET);
  await expect(page.locator("body")).not.toContainText(generated);
  await expectNoViolations(page, "Webhook delivery");

  await page.getByRole("button", { name: "Resend", exact: true }).click();
  const resend = page.getByRole("dialog", { name: `Resend ping to ${HOOK}?` });
  await expectNoViolations(page, "Resend dialog");
  await resend.getByRole("button", { name: `Resend to ${HOOK}` }).click();
  await expect(resend).toHaveCount(0);
  await expect(page.getByRole("link", { name: "The original delivery" })).toBeVisible();
  await expect(page.getByText("No attempt yet.")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(`ping to ${HOOK}`);
  await expect(page.locator("main")).toContainText("Queued");
  // The worker's next tick sends it.
  await tick(page);
  await page.reload();
  await expect(page.getByRole("region", { name: "Attempts table" })).toContainText("HTTP 202");
  await page.context().close();
});

test("Edit the events, then rotate the signing secret with a grace period", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await page.goto("/admin/webhooks");
  const row = rowOf(page, HOOK);
  await row.getByRole("button", { name: `Edit ${HOOK}` }).click();
  const dialog = page.getByRole("dialog", { name: `Edit webhook ${HOOK}` });
  await expect(dialog.getByText(/Address set on .*\(hooks\.e2e\.test\)/)).toBeVisible();
  await expect(dialog.getByLabel("Replace address")).toHaveValue("");
  await expect(dialog.getByLabel("Replace signing secret")).toHaveValue("");
  const records = dialog.getByRole("group", { name: "Records" });
  await records.getByLabel(/^Every records event/).uncheck();
  await records.getByLabel(/^record\.updated/).check();
  await expect(dialog.getByText("Chosen: renewal.decided, record.updated.")).toBeVisible();
  await expectNoViolations(page, "Edit webhook dialog");
  await dialog.getByRole("button", { name: "Save webhook" }).click();
  await expect(dialog).toHaveCount(0);
  await said(page, `Webhook ${HOOK} saved.`);
  // The API keeps subscriptions in the catalog's order.
  await expect(row).toContainText("record.updated, renewal.decided");
  await expect(row).not.toContainText("record.*");

  const more = row.getByRole("button", { name: `More for ${HOOK}` });
  await more.click();
  await page.getByRole("menuitem", { name: "Rotate secret…" }).click();
  const rotate = page.getByRole("dialog", { name: `Rotate the signing secret of ${HOOK}` });
  await expect(rotate.getByText(/Current secret set on/)).toBeVisible();
  await rotate.getByRole("button", { name: "Rotate secret" }).click();
  await expect(rotate.getByRole("alert")).toContainText("16 characters or more");
  await rotate.getByLabel("New signing secret (required)").fill(SIGNING);
  await rotate.getByLabel("Grace period").selectOption({ label: "3 days" });
  await expectNoViolations(page, "Rotate secret dialog");
  await rotate.getByRole("button", { name: "Rotate secret" }).click();
  await expect(rotate).toHaveCount(0);
  await said(page, `Signing secret of ${HOOK} rotated. The previous secret also signs until`);
  await expect(row).toContainText("Previous secret signs until");
  await expect(more).toBeFocused();
  await page.context().close();
});

test("A change made while edit, rotate or delete is open is refused (412) and offers the latest version", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  const hooks = await (await page.request.get("/api/v1/webhooks")).json() as { id: string; name: string }[];
  const id = hooks.find((h) => h.name === HOOK)!.id;
  const meanwhile = (description: string) => post(page, `webhooks/${id}`, { description }, "PATCH");
  await page.goto("/admin/webhooks");
  const row = rowOf(page, HOOK);

  await row.getByRole("button", { name: `Edit ${HOOK}` }).click();
  const edit = page.getByRole("dialog", { name: `Edit webhook ${HOOK}` });
  await expect(edit.getByLabel("Description")).toHaveValue("The service desk's intake");
  await meanwhile("Changed in another tab");
  await edit.getByLabel("Name (required)").fill(`${HOOK} renamed`);
  await edit.getByRole("button", { name: "Save webhook" }).click();
  await expect(edit.getByRole("alert")).toContainText("Someone changed this webhook since you opened it. Nothing was saved.");
  await expectNoViolations(page, "Edit webhook, changed meanwhile");
  await edit.getByRole("button", { name: "Load the latest version" }).click();
  await expect(edit.getByLabel("Description")).toHaveValue("Changed in another tab");
  await expect(edit.getByLabel("Name (required)")).toHaveValue(HOOK);
  await edit.getByRole("button", { name: "Cancel" }).click();

  await row.getByRole("button", { name: `More for ${HOOK}` }).click();
  await page.getByRole("menuitem", { name: "Rotate secret…" }).click();
  const rotate = page.getByRole("dialog", { name: `Rotate the signing secret of ${HOOK}` });
  await expect(rotate.getByText(/Current secret set on/)).toBeVisible();
  await meanwhile("Changed again");
  await rotate.getByLabel("New signing secret (required)").fill(`${SIGNING}-next`);
  await rotate.getByRole("button", { name: "Rotate secret" }).click();
  await expect(rotate.getByRole("alert")).toContainText("Nothing was rotated.");
  await rotate.getByRole("button", { name: "Cancel" }).click();

  await row.getByRole("button", { name: `More for ${HOOK}` }).click();
  await page.getByRole("menuitem", { name: "Delete…" }).click();
  const remove = page.getByRole("dialog", { name: `Delete webhook ${HOOK}?` });
  await expect(remove.getByRole("button", { name: `Delete ${HOOK}` })).toBeEnabled();
  await meanwhile("Changed a third time");
  await remove.getByRole("button", { name: `Delete ${HOOK}` }).click();
  await expect(remove.getByRole("alert")).toContainText("Nothing was deleted.");
  // Loading the latest version reloads the list behind the dialog too, which shows the same change.
  await remove.getByRole("button", { name: "Load the latest version" }).click();
  await expect(remove.getByRole("alert")).toHaveCount(0);
  await remove.getByRole("button", { name: "Cancel" }).click();
  // Nothing else reloads the list on Cancel: the change shows because "Load the latest version" reloaded it.
  await expect(rowOf(page, HOOK)).toContainText("Changed a third time");
  await expect(rowOf(page, HOOK)).toHaveCount(1);
  await page.context().close();
});

test("Without the webhooks capability the pages say so and call nothing", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  // Every edition has webhooks today, so the organization's capabilities are answered without it: the SPA gates on
  // `auth/me.capabilities` alone, and that gate is what this checks.
  await page.route("**/api/v1/auth/me", async (route) => {
    const response = await route.fetch();
    const me = await response.json() as { capabilities: string[] };
    await route.fulfill({ response, json: { ...me, capabilities: me.capabilities.filter((c) => c !== "webhooks") } });
  });
  const asked: string[] = [];
  page.on("request", (r) => { if (/\/api\/v1\/(webhooks|webhook-deliveries)/.test(r.url())) asked.push(r.url()); });
  for (const [path, title] of [["/admin/webhooks", "Webhooks"], ["/admin/webhooks/deliveries", "Webhook delivery log"]]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await expect(page.locator("[data-unavailable]")).toContainText("Webhooks aren't available.");
  }
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Webhooks" })).toHaveCount(0);
  await expectNoViolations(page, "Webhook log without the capability");
  expect(asked).toEqual([]);
  await page.context().close();
});

test("The delivery log: a failed ping and event deliveries, filtered in the URL", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  const broken = await post(page, "webhooks", { name: BROKEN, url: "https://fail.e2e.test/hook", signing_secret: SIGNING, events: ["record.*"] }) as { id: string };
  await page.goto("/admin/webhooks");
  const row = rowOf(page, BROKEN);
  await row.getByRole("button", { name: `Ping ${BROKEN}` }).click();
  await expect(page.getByTestId("ping-result")).toContainText(`Ping to ${BROKEN} failed`);
  await expect(page.getByTestId("ping-result")).toContainText("HTTP 500");
  // A change the endpoint subscribes to is queued for it (record.*), and none for the first (record.updated only).
  await post(page, "applications", { name: `${t} Payroll`, lifecycle: "active" });
  await post(page, "e2e/run-digest", { outbox: true }); // the worker's outbox drain, no delivery attempted

  await page.goto("/admin/webhooks/deliveries");
  await expect(page.getByRole("heading", { level: 1, name: "Webhook delivery log" })).toBeVisible();
  await page.getByLabel("Status").selectOption("failed");
  await expect(page).toHaveURL(/status=failed/);
  const log = page.getByRole("region", { name: "Webhook delivery log table" });
  await expect(log.getByRole("row").filter({ hasText: BROKEN }).first()).toContainText("ping");
  await expect(log).not.toContainText(HOOK);
  await expectNoViolations(page, "Webhook delivery log, failed");

  await page.getByLabel("Status").selectOption("");
  await page.getByLabel("Endpoint").selectOption({ label: BROKEN });
  await page.getByLabel("Kind").selectOption({ label: "Event" });
  await expect(page).toHaveURL(new RegExp(`webhook=${broken.id}`));
  await expect(page).toHaveURL(/kind=event/);
  const event = log.getByRole("row").filter({ hasText: "record.created" });
  await expect(event).toContainText("Queued");
  await expect(event).toContainText("0 of 6");
  // Nothing to resend while it is queued.
  await expect(event.getByRole("button", { name: /^Resend/ })).toHaveCount(0);

  // The filters are in the address: a reload keeps them.
  await page.reload();
  await expect(page.getByLabel("Endpoint")).toHaveValue(broken.id);
  await expect(page.getByLabel("Kind")).toHaveValue("event");
  await expect(log.getByRole("row").filter({ hasText: "record.created" })).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).toHaveURL(/\/admin\/webhooks\/deliveries$/);

  await page.goto(`/admin/webhooks/deliveries?webhook=${broken.id}&event=renewal.*`);
  await expect(page.getByText("No deliveries match these filters")).toBeVisible();
  await expectNoViolations(page, "Webhook delivery log, no matches");
  await page.context().close();
});

test("An endpoint Softwarden turned off: the Admin banner and the reason, until it is turned back on", async ({ browser, baseURL }) => {
  const page = await as(browser, baseURL, ADMIN);
  await post(page, "webhooks", { name: GONE, url: "https://gone.e2e.test/hook", signing_secret: SIGNING, events: ["record.*"] });
  await post(page, "applications", { name: `${t} Ledger`, lifecycle: "active" });
  await tick(page); // the endpoint answers 410 Gone: Softwarden turns it off
  await page.goto("/admin/webhooks");
  const banner = page.getByTestId("webhook-problem");
  await expect(banner).toContainText(`Softwarden turned off the webhook ${GONE}`);
  await expect(banner.getByRole("link", { name: "Review webhooks" })).toBeVisible();
  const row = rowOf(page, GONE);
  await expect(row).toContainText("Turned off by Softwarden");
  await expect(row).toContainText("410");
  await expectNoViolations(page, "Webhooks with an endpoint turned off");

  await row.getByRole("button", { name: `Ping ${GONE}` }).click();
  await expect(page.getByTestId("ping-result")).toContainText(`Ping to ${GONE} not sent`);

  await row.getByRole("button", { name: `Edit ${GONE}` }).click();
  const dialog = page.getByRole("dialog", { name: `Edit webhook ${GONE}` });
  await expect(dialog.getByRole("note")).toContainText("Softwarden turned this endpoint off");
  await dialog.getByLabel("On (Softwarden posts events to it)").check();
  await dialog.getByRole("button", { name: "Save webhook" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(banner).toHaveCount(0);
  await expect(row.getByText("Turned off by Softwarden")).toHaveCount(0);
  await expect(row).toContainText("On");

  // Deleting it keeps its deliveries in the log.
  await row.getByRole("button", { name: `More for ${GONE}` }).click();
  await page.getByRole("menuitem", { name: "Delete…" }).click();
  const remove = page.getByRole("dialog", { name: `Delete webhook ${GONE}?` });
  await remove.getByRole("button", { name: `Delete ${GONE}` }).click();
  await said(page, `Webhook ${GONE} deleted. Its deliveries stay in the delivery log.`);
  await expect(rowOf(page, GONE)).toHaveCount(0);
  await page.goto("/admin/webhooks/deliveries?kind=event");
  await expect(page.getByRole("region", { name: "Webhook delivery log table" }).getByRole("row").filter({ hasText: GONE }).first()).toContainText("Endpoint deleted");
  await page.context().close();
});
