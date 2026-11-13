import { expect, test } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

test.describe.configure({ mode: "serial" });

test("Editor adds, edits and removes a relationship; link state is text and the other side shows it", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const portal = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const payroll = await post(page, "components", { name: `${t} Payroll Sync`, lifecycle: "active", component_type: "script" });
  await post(page, `applications/${portal.id}/instances`, { name: `${t} Production`, environment: "production" });

  await page.goto(`/applications/${portal.id}`);
  await page.getByRole("tab", { name: /^Relationships/ }).click();
  await expect(page).toHaveURL(/tab=relationships/);
  // Empty states, with the exact Used-by copy.
  await expect(page.getByText(`Nothing is recorded as depending on ${t} Portal. That doesn't mean nothing does.`)).toBeVisible();
  await expect(page.getByText(`${t} Portal has no recorded dependencies.`)).toBeVisible();

  const open = page.getByRole("button", { name: "Add relationship" });
  await open.click();
  const dialog = page.getByRole("dialog", { name: `Add relationship — ${t} Portal` });
  await dialog.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} Payroll`);
  await dialog.getByRole("option", { name: new RegExp(`${t} Payroll Sync`) }).click();
  await dialog.getByLabel(`Instance of ${t} Portal (optional)`).selectOption({ label: `${t} Production (Production)` });
  await dialog.getByLabel("Purpose").fill("Nightly export");
  await dialog.getByRole("button", { name: "Add relationship" }).click();
  await expect(dialog).toBeHidden();
  await expect(open).toBeFocused();

  const depends = page.getByRole("region", { name: "Depends on table" });
  const row = depends.getByRole("row", { name: new RegExp(`${t} Payroll Sync`) });
  await expect(row.getByText("Unconfirmed")).toBeVisible();
  await expect(row.getByText("Never confirmed.")).toBeVisible();
  await expect(row.getByText(`${t} Production`)).toBeVisible();
  await expect(row.getByText("Nightly export")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Relationships (1)" })).toBeVisible();

  // The same link, read from the other side.
  await page.goto(`/components/${payroll.id}?tab=relationships`);
  await expect(page.getByRole("region", { name: "Is needed by table" }).getByRole("link", { name: `${t} Portal` })).toBeVisible();

  // Edit: change the purpose; duplicates are refused with a message.
  await page.goto(`/applications/${portal.id}?tab=relationships`);
  const edit = depends.getByRole("button", { name: `Edit link with ${t} Payroll Sync` });
  await edit.click();
  const editDialog = page.getByRole("dialog", { name: `Edit relationship — ${t} Portal` });
  await editDialog.getByLabel("Purpose").fill("Weekly export");
  await editDialog.getByRole("button", { name: "Save" }).click();
  await expect(editDialog).toBeHidden();
  await expect(edit).toBeFocused();
  await expect(depends.getByText("Weekly export")).toBeVisible();

  await page.getByRole("button", { name: "Add relationship" }).click();
  const again = page.getByRole("dialog", { name: `Add relationship — ${t} Portal` });
  await again.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} Payroll`);
  await again.getByRole("option", { name: new RegExp(`${t} Payroll Sync`) }).click();
  await again.getByLabel(`Instance of ${t} Portal (optional)`).selectOption({ label: `${t} Production (Production)` });
  await again.getByRole("button", { name: "Add relationship" }).click();
  await expect(again.getByRole("alert")).toContainText("This link is already recorded.");
  await again.getByRole("button", { name: "Cancel" }).click();

  // Remove.
  await depends.getByRole("button", { name: `Remove link with ${t} Payroll Sync` }).click();
  await page.getByRole("dialog", { name: "Remove this relationship?" }).getByRole("button", { name: "Remove relationship" }).click();
  await expect(page.getByText("Relationship removed.").first()).toBeVisible();
  await expect(page.getByText(`${t} Portal has no recorded dependencies.`)).toBeVisible();
});

test("Replaces and implements: a component can implement an integration; kinds are limited", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const oldApp = await post(page, "applications", { name: `${t} Old`, lifecycle: "deprecated" });
  const newApp = await post(page, "applications", { name: `${t} New`, lifecycle: "active" });
  const comp = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: oldApp.id, target: newApp.id });

  await page.goto(`/applications/${newApp.id}?tab=relationships`);
  await page.getByRole("button", { name: "Add relationship" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Relationship (required)").selectOption({ label: `${t} New replaces…` });
  await dialog.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} Old`);
  await dialog.getByRole("option", { name: new RegExp(`${t} Old`) }).click();
  await dialog.getByRole("button", { name: "Add relationship" }).click();
  await expect(dialog).toBeHidden();
  const replaces = page.getByRole("region", { name: "Replaces / is replaced by table" });
  await expect(replaces.getByRole("row", { name: new RegExp(`${t} Old`) }).getByText("Replaces", { exact: true })).toBeVisible();
  // Applications cannot implement an integration: no Implements section.
  await expect(page.getByRole("heading", { name: /^Implements/ })).toHaveCount(0);

  await page.goto(`/components/${comp.id}?tab=relationships`);
  await expect(page.getByText(`${t} Sync doesn't implement any integration.`)).toBeVisible();
  await page.getByRole("button", { name: "Add relationship" }).click();
  const impl = page.getByRole("dialog");
  await impl.getByLabel("Relationship (required)").selectOption({ label: `${t} Sync implements… (an integration)` });
  await impl.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} Flow`);
  await impl.getByRole("option", { name: new RegExp(`${t} Flow`) }).click();
  await impl.getByRole("button", { name: "Add relationship" }).click();
  await expect(impl).toBeHidden();
  await expect(page.getByRole("region", { name: "Implements table" }).getByRole("link", { name: `${t} Flow` })).toBeVisible();
});

test("API refusals show on the field: a component can't depend on itself", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const comp = await post(page, "components", { name: `${t} Loop`, lifecycle: "active", component_type: "script" });
  await page.goto(`/components/${comp.id}?tab=relationships`);
  await page.getByRole("button", { name: "Add relationship" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("combobox", { name: "Other record (required)" }).fill(`${t} Loop`);
  await dialog.getByRole("listbox").getByRole("option", { name: new RegExp(`${t} Loop`) }).click();
  await dialog.getByRole("button", { name: "Add relationship" }).click();
  await expect(dialog.getByRole("alert")).toContainText("A record can only link to itself through two different instances.");
  await expect(dialog.getByRole("combobox", { name: "Other record (required)" })).toHaveAttribute("aria-invalid", "true");
});

test("Viewer sees relationships read-only", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const a = await post(editor, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(editor, "applications", { name: `${t} B`, lifecycle: "active" });
  await post(editor, "relationships", { type: "depends_on", source: a.id, target: b.id });
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${a.id}?tab=relationships`);
  const table = viewer.getByRole("region", { name: "Depends on table" });
  await expect(table.getByRole("link", { name: `${t} B` })).toBeVisible();
  await expect(table.getByText("Unconfirmed")).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Add relationship" })).toHaveCount(0);
  await expect(table.getByRole("button", { name: /^Edit|^Remove/ })).toHaveCount(0);
});

test("Data flows section lists integrations in and out, labeled as not operational", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const hub = await post(page, "applications", { name: `${t} Hub`, lifecycle: "active" });
  const lonely = await post(page, "applications", { name: `${t} Lonely`, lifecycle: "active" });
  const ads = await post(page, "applications", { name: `${t} ADP`, lifecycle: "active" });
  const comp = await post(page, "components", { name: `${t} Carrier`, lifecycle: "active", component_type: "script" });
  const outgoing = await post(page, "integrations", { name: `${t} Hub to ADP`, lifecycle: "active", source: hub.id, target: ads.id, protocol: "SFTP" });
  await post(page, "integrations", { name: `${t} ADP to Hub`, lifecycle: "active", source: ads.id, target: hub.id });
  await post(page, "relationships", { type: "implements", source: comp.id, target: outgoing.id });

  await page.goto(`/applications/${hub.id}?tab=relationships`);
  const section = page.locator('section[aria-labelledby="h-rel-flows"]');
  await expect(section).toContainText("data flow, not operational");
  const table = section.getByRole("region", { name: "Data flows table" });
  const out = table.getByRole("row", { name: new RegExp(`${t} Hub to ADP`) });
  await expect(out).toContainText(`Sends data to ${t} ADP`);
  await expect(out).toContainText(`${t} Carrier`);
  await expect(out).toContainText("SFTP");
  await expect(out).toContainText("Unconfirmed");
  const into = table.getByRole("row", { name: new RegExp(`${t} ADP to Hub`) });
  await expect(into).toContainText(`Receives data from ${t} ADP`);
  await expect(into).toContainText("Vendor-native");
  // They are not operational dependencies: the dependency sections stay empty.
  await expect(page.getByText(`${t} Hub has no recorded dependencies.`)).toBeVisible();
  await out.getByRole("link", { name: `${t} Hub to ADP` }).click();
  await expect(page).toHaveURL(/\/integrations\/[0-9a-f-]+$/);

  await page.goto(`/applications/${lonely.id}?tab=relationships`);
  await expect(page.getByText(`No data flows are recorded for ${t} Lonely.`)).toBeVisible();
  await page.goto(`/components/${comp.id}?tab=relationships`);
  await expect(page.locator('section[aria-labelledby="h-rel-flows"]')).toContainText(`No data flows are recorded for ${t} Carrier.`);
});
