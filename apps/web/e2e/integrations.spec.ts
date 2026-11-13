import { expect, test } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

test.describe.configure({ mode: "serial" });

test("Editor creates an integration, adds and removes an implementer, edits it and sets ownership", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const source = await post(page, "applications", { name: `${t} Workday`, lifecycle: "active" });
  await post(page, "applications", { name: `${t} ADP`, lifecycle: "active" });
  await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  await post(page, `applications/${source.id}/instances`, { name: `${t} Production`, environment: "production" });

  // The nav entry sits under Catalog and opens the list, which starts with an empty state or rows.
  await page.goto("/");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Integrations" }).click();
  await expect(page.getByRole("heading", { name: "Integrations", level: 1 })).toBeVisible();
  // The header button; an empty list also shows an "Add integration" link in its empty state.
  await page.getByRole("link", { name: "Add integration" }).first().click();

  // Create form: required fields are reported together, then a valid save lands on the detail page.
  await page.getByRole("button", { name: "Save" }).click();
  const summary = page.getByRole("alert").filter({ hasText: "Fix these fields" });
  await expect(summary).toContainText("Enter a name.");
  await expect(summary).toContainText("Choose a record.");
  await page.getByLabel("Name (required)").fill(`${t} Payroll feed`);
  await page.getByRole("combobox", { name: "Source (required)" }).fill(`${t} Workday`);
  await page.getByRole("listbox").getByRole("option", { name: new RegExp(`${t} Workday`) }).click();
  await page.getByLabel(`Instance of ${t} Workday (optional)`).selectOption({ label: `${t} Production (Production)` });
  await page.getByRole("combobox", { name: "Target (required)" }).fill(`${t} ADP`);
  await page.getByRole("listbox").getByRole("option", { name: new RegExp(`${t} ADP`) }).click();
  await page.getByLabel("Protocol").fill("SFTP");
  await page.getByLabel("Data purpose").fill("Payroll totals");
  await page.getByRole("combobox", { name: "Technical owner" }).fill("Dana");
  await page.getByRole("listbox").getByRole("option", { name: /Dana Ortiz/ }).click();
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page).toHaveURL(/\/integrations\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { level: 1, name: `${t} Payroll feed` })).toBeVisible();
  const status = page.getByRole("group", { name: "Integration status" });
  await expect(status.getByText("Unconfirmed")).toBeVisible();
  await expect(status.getByText("Never confirmed.")).toBeVisible();
  await expect(status.getByText("No support team")).toBeVisible();
  await expect(status.getByText("No technical owner")).toHaveCount(0);
  const flow = page.getByRole("region", { name: /^Data flow/ });
  await expect(flow).toContainText("data flow, not operational");
  await expect(flow.getByRole("link", { name: `${t} Workday` })).toBeVisible();
  await expect(flow).toContainText(`(${t} Production)`);
  // No business owner or criticality on an integration.
  await expect(page.getByText("Business owner")).toHaveCount(0);
  await expect(page.getByText("Criticality")).toHaveCount(0);

  // Vendor-native until a component implements it.
  const impl = page.locator('section[aria-labelledby="h-impl"]');
  await expect(impl).toContainText("Vendor-native: no component in this catalog implements this integration.");
  await expect(page.getByText("Vendor-native").first()).toBeVisible();
  const add = impl.getByRole("button", { name: "Add implementer" });
  await add.click();
  const dialog = page.getByRole("dialog", { name: `Add implementer — ${t} Payroll feed` });
  await dialog.getByRole("button", { name: "Add implementer" }).click();
  await expect(dialog.getByRole("alert").first()).toContainText("Choose a component.");
  await dialog.getByRole("combobox", { name: "Component (required)" }).fill(`${t} Sync`);
  await dialog.getByRole("listbox").getByRole("option", { name: new RegExp(`${t} Sync`) }).click();
  await dialog.getByLabel("Purpose").fill("Runs nightly");
  await dialog.getByRole("button", { name: "Add implementer" }).click();
  await expect(dialog).toBeHidden();
  await expect(add).toBeFocused();
  const table = impl.getByRole("region", { name: "Implemented by table" });
  await expect(table.getByRole("link", { name: `${t} Sync` })).toBeVisible();
  await expect(table.getByText("Runs nightly")).toBeVisible();
  await expect(page.getByText("Vendor-native", { exact: true })).toHaveCount(0);

  await table.getByRole("button", { name: `Remove implementer ${t} Sync` }).click();
  await page.getByRole("dialog", { name: "Remove this relationship?" }).getByRole("button", { name: "Remove relationship" }).click();
  await expect(impl).toContainText("Vendor-native: no component in this catalog implements this integration.");
  await expect(page.getByText("Implementer removed.").first()).toBeVisible();

  // Edit the flow itself.
  await page.getByRole("link", { name: `Edit ${t} Payroll feed` }).click();
  await expect(page.getByLabel("Name (required)")).toHaveValue(`${t} Payroll feed`);
  await expect(page.getByText("changed with Edit ownership")).toBeVisible();
  await page.getByLabel("Schedule").fill("Nightly 02:00");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("heading", { level: 1, name: `${t} Payroll feed` })).toBeVisible();
  await expect(page.getByText("Nightly 02:00")).toBeVisible();

  // Ownership uses the owner dialog, without business owner or criticality, and has Undo.
  await page.getByRole("button", { name: "Assign support team" }).click();
  const owners = page.getByRole("dialog", { name: `Assign owners — ${t} Payroll feed` });
  await expect(owners.getByRole("combobox", { name: "Support team" })).toBeFocused();
  await expect(owners.getByLabel("Business owner")).toHaveCount(0);
  await expect(owners.getByLabel("Criticality")).toHaveCount(0);
  await owners.getByRole("combobox", { name: "Support team" }).fill("IT Applications");
  await owners.getByRole("listbox").getByRole("option", { name: "IT Applications" }).click();
  await owners.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Support team set to IT Applications." })).toBeVisible();
  await expect(page.getByRole("group", { name: "Integration status" }).getByText("✓ Complete")).toBeVisible();
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByRole("group", { name: "Integration status" }).getByText("No support team")).toBeVisible();

  // Audit activity records the create, the edit and the implementer link.
  await page.getByRole("tab", { name: "Audit activity" }).click();
  await expect(page).toHaveURL(/tab=activity/);
  const log = page.getByRole("region", { name: "Audit activity table" });
  await expect(log.getByRole("cell", { name: "Record created" })).toBeVisible();
  await expect(log.getByRole("cell", { name: "Relationship added" })).toBeVisible();
  await expect(log.getByRole("cell", { name: "Relationship removed" })).toBeVisible();
  await expect(log.getByText(/Schedule: not set → Nightly 02:00/)).toBeVisible();
});

test("List: search, vendor-native filter, empty states and archive keep their state in the URL", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const a = await post(page, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(page, "applications", { name: `${t} B`, lifecycle: "active" });
  const comp = await post(page, "components", { name: `${t} Carrier`, lifecycle: "active", component_type: "script" });
  const native = await post(page, "integrations", { name: `${t} Native`, lifecycle: "active", source: a.id, target: b.id });
  const own = await post(page, "integrations", { name: `${t} Ours`, lifecycle: "active", source: b.id, target: a.id });
  await post(page, "relationships", { type: "implements", source: comp.id, target: own.id });

  await page.goto(`/integrations?q=${encodeURIComponent(t)}`);
  const table = page.getByRole("region", { name: "Integrations table" });
  await expect(table.getByRole("rowheader", { name: `${t} Native` })).toBeVisible();
  await expect(table.getByRole("row", { name: new RegExp(`${t} Ours`) })).toContainText(`${t} Carrier`);
  await expect(table.getByRole("row", { name: new RegExp(`${t} Native`) })).toContainText("Vendor-native");
  await expect(table.getByRole("row", { name: new RegExp(`${t} Native`) })).toContainText("No technical owner");

  await page.getByRole("button", { name: "Vendor-native" }).click();
  await expect(page).toHaveURL(/preset=vendor-native/);
  await expect(table.getByRole("rowheader", { name: `${t} Native` })).toBeVisible();
  await expect(table.getByRole("rowheader", { name: `${t} Ours` })).toHaveCount(0);

  // Archive from the detail page; it moves to the Archived filter.
  await page.goto(`/integrations/${native.id}`);
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("menuitem", { name: "Archive" }).click();
  await page.getByRole("dialog", { name: `Archive ${t} Native?` }).getByRole("button", { name: "Archive", exact: true }).click();
  await expect(page.getByRole("region", { name: "Archived integration" })).toBeVisible();
  await page.goto(`/integrations?preset=archived&q=${encodeURIComponent(t)}`);
  await expect(table.getByRole("rowheader", { name: `${t} Native` })).toBeVisible();
  await page.goto(`/integrations?q=${"qqqjjjwwwkkk"}`);
  await expect(page.getByText("No integrations match these filters")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).first().click();
  await expect(page).toHaveURL(/\/integrations$/);
});

test("Delete is refused while a component implements the integration; Viewer is read-only", async ({ browser, baseURL }) => {
  const t = token();
  const admin = await pageAs(browser, baseURL, "admin@example.test");
  const a = await post(admin, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(admin, "applications", { name: `${t} B`, lifecycle: "active" });
  const comp = await post(admin, "components", { name: `${t} Carrier`, lifecycle: "active", component_type: "script" });
  const flow = await post(admin, "integrations", { name: `${t} Flow`, lifecycle: "active", source: a.id, target: b.id });
  await post(admin, "relationships", { type: "implements", source: comp.id, target: flow.id });

  await admin.goto(`/integrations/${flow.id}`);
  await admin.getByRole("button", { name: "More" }).click();
  await admin.getByRole("menuitem", { name: "Delete" }).click();
  const dialog = admin.getByRole("dialog", { name: `Delete ${t} Flow?` });
  await dialog.getByLabel("Name (required)").fill(`${t} Flow`);
  await dialog.getByRole("button", { name: `Delete ${t} Flow` }).click();
  await expect(dialog.getByRole("alert")).toContainText("can't be deleted because it has 1 relationship");

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/integrations/${flow.id}`);
  await expect(viewer.getByRole("heading", { level: 1, name: `${t} Flow` })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Add implementer" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: "Edit ownership" })).toHaveCount(0);
  await expect(viewer.getByRole("link", { name: `Edit ${t} Flow` })).toHaveCount(0);
  await viewer.goto("/integrations");
  await expect(viewer.getByRole("link", { name: "Add integration" })).toHaveCount(0);
  await viewer.goto("/integrations/new");
  await expect(viewer.getByRole("heading", { name: "You can't change integrations" })).toBeVisible();
});

test("The integration form asks before discarding unsaved changes", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const source = await post(page, "applications", { name: `${t} Source`, lifecycle: "active" });
  const target = await post(page, "components", { name: `${t} Target`, lifecycle: "active", component_type: "script" });
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: source.id, target: target.id });

  await page.goto(`/integrations/${flow.id}/edit`);
  await page.getByLabel("Name (required)").fill(`${t} Renamed`);
  const cancel = page.getByRole("link", { name: "Cancel" });
  await cancel.click();
  const dialog = page.getByRole("dialog", { name: "Discard changes?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(dialog).toBeHidden();
  await expect(cancel).toBeFocused(); // focus returns to the control that was activated
  await expect(page).toHaveURL(new RegExp(`/integrations/${flow.id}/edit$`));

  await cancel.click();
  await page.getByRole("dialog", { name: "Discard changes?" }).getByRole("button", { name: "Discard changes" }).click();
  await expect(page).toHaveURL(new RegExp(`/integrations/${flow.id}$`));
  await expect(page.getByRole("heading", { name: `${t} Flow`, level: 1 })).toBeVisible();

  // Saving is not "discarding": no prompt, and a clean form leaves without one.
  await page.goto(`/integrations/${flow.id}/edit`);
  await page.getByRole("link", { name: "Cancel" }).click();
  await expect(page).toHaveURL(new RegExp(`/integrations/${flow.id}$`));
  await page.goto(`/integrations/${flow.id}/edit`);
  await page.getByLabel("Name (required)").fill(`${t} Saved`);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page).toHaveURL(new RegExp(`/integrations/${flow.id}$`));
  await expect(page.getByRole("heading", { name: `${t} Saved`, level: 1 })).toBeVisible();
});
