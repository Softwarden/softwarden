import { expect, test } from "@playwright/test";
import { pageAs, personId, post, token } from "./helpers";

test.describe.configure({ mode: "serial" });

test("Confirm a relationship: validation, inactive confirmer needs a reason, focus returns, history shows it", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const a = await post(page, "applications", { name: `${t} Portal`, lifecycle: "active" });
  const b = await post(page, "components", { name: `${t} Sync`, lifecycle: "active", component_type: "script" });
  await post(page, "relationships", { type: "depends_on", source: a.id, target: b.id });
  const former = await post(page, "people", { name: `${t} Former`, email: `${t.toLowerCase()}@example.test` });
  await post(page, `people/${former.id}`, { active: false }, "PATCH");

  await page.goto(`/applications/${a.id}?tab=relationships`);
  const table = page.getByRole("region", { name: "Depends on table" });
  const row = table.getByRole("row", { name: new RegExp(`${t} Sync`) });
  await expect(row.getByText("Unconfirmed")).toBeVisible();
  await expect(row.getByText("Never confirmed.")).toBeVisible();

  // History is empty before the first confirmation.
  const historyButton = row.getByRole("button", { name: `History of confirmations for link with ${t} Sync` });
  await historyButton.click();
  const history = page.getByRole("dialog", { name: "Confirmation history" });
  await expect(history.getByText("Never confirmed")).toBeVisible();
  await history.getByRole("button", { name: "Close" }).click();
  await expect(historyButton).toBeFocused();

  const opener = row.getByRole("button", { name: `Confirm link with ${t} Sync` });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Confirm link" });
  await expect(dialog).toContainText(`${t} Portal depends on ${t} Sync`);
  await expect(dialog).toContainText("Never confirmed.");
  // The Record review extras do not belong here: no confirmed-values summary and no next review date.
  await expect(dialog.getByText("Business owner")).toHaveCount(0);
  await expect(dialog.getByLabel(/Next review/)).toHaveCount(0);

  await dialog.getByRole("button", { name: "Confirm link" }).click();
  const summary = dialog.getByRole("alert").filter({ hasText: "Fix these fields" });
  await expect(summary).toContainText("Choose who confirmed it.");
  await expect(summary).toContainText("Describe how you got the confirmation.");

  await dialog.getByLabel("Confirmed by (required)").selectOption({ label: `${t} Former (inactive)` });
  await dialog.getByLabel("Evidence / reference (required)").fill("Email from the former owner");
  await dialog.getByRole("button", { name: "Confirm link" }).click();
  await expect(dialog.getByRole("alert").filter({ hasText: "Fix this field" })).toContainText("Give a reason for using an inactive person.");
  await dialog.getByLabel("Reason for using an inactive person (required)").fill("Owner left; manager vouched");
  await dialog.getByRole("button", { name: "Confirm link" }).click();

  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  await expect(page.getByText("Link confirmed.").first()).toBeVisible();
  await expect(row.getByText("✓Confirmed", { exact: true })).toBeVisible();
  await expect(row.getByText(/^Confirmed \d{4}-\d{2}-\d{2}\.$/)).toBeVisible();

  await historyButton.click();
  const filled = page.getByRole("dialog", { name: "Confirmation history" });
  const historyRow = filled.getByRole("row", { name: new RegExp(`${t} Former`) });
  await expect(historyRow).toContainText("(inactive)");
  await expect(historyRow).toContainText("Email from the former owner");
  await expect(historyRow).toContainText("Reason: Owner left; manager vouched");
  await page.keyboard.press("Escape");
  await expect(historyButton).toBeFocused();

  // Changing the source record's technical owner changes the link's signature: it needs verifying again.
  const dana = await personId(page, "Dana Ortiz");
  await post(page, `applications/${a.id}`, { technical_owner: dana }, "PATCH");
  await page.reload();
  const again = page.getByRole("region", { name: "Depends on table" }).getByRole("row", { name: new RegExp(`${t} Sync`) });
  await expect(again.getByText("Needs verification")).toBeVisible();
  await expect(again.getByText(/Owner changed since confirmation/)).toBeVisible();
});

test("Confirm an integration from its page; the history tab and status bar follow", async ({ browser, baseURL }) => {
  const t = token();
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const a = await post(page, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(page, "applications", { name: `${t} B`, lifecycle: "active" });
  const flow = await post(page, "integrations", { name: `${t} Flow`, lifecycle: "active", source: a.id, target: b.id });

  await page.goto(`/integrations/${flow.id}`);
  await expect(page.getByRole("tab", { name: "Confirmation history" })).toBeVisible();
  await page.getByRole("tab", { name: "Confirmation history" }).click();
  await expect(page).toHaveURL(/tab=history/);
  await expect(page.getByText("Never confirmed", { exact: true })).toBeVisible();

  const opener = page.getByRole("button", { name: "Confirm link", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Confirm link" });
  await expect(dialog).toContainText(`${t} Flow`);
  await dialog.getByLabel("Confirmed by (required)").selectOption({ label: "Priya Raman" });
  await dialog.getByLabel("Evidence / reference (required)").fill("Checked with HR");
  await dialog.getByRole("button", { name: "Confirm link" }).click();
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();

  await expect(page.getByRole("tab", { name: "Confirmation history (1)" })).toBeVisible();
  const status = page.getByRole("group", { name: "Integration status" });
  await expect(status.getByText("✓Confirmed", { exact: true })).toBeVisible();
  const history = page.getByRole("region", { name: "Confirmation history table" });
  await expect(history.getByRole("row", { name: /Priya Raman/ })).toContainText("Checked with HR");

  // Changing an end re-points the link: needs verification again.
  const c = await post(page, "applications", { name: `${t} C`, lifecycle: "active" });
  await post(page, `integrations/${flow.id}`, { target: c.id }, "PATCH");
  await page.reload();
  await expect(page.getByRole("group", { name: "Integration status" }).getByText("Needs verification")).toBeVisible();

  // The implementer link has its own confirm and history controls.
  const comp = await post(page, "components", { name: `${t} Carrier`, lifecycle: "active", component_type: "script" });
  await post(page, "relationships", { type: "implements", source: comp.id, target: flow.id });
  await page.goto(`/integrations/${flow.id}`);
  const impl = page.getByRole("region", { name: "Implemented by table" });
  await impl.getByRole("button", { name: `Confirm link with implementer ${t} Carrier` }).click();
  const second = page.getByRole("dialog", { name: "Confirm link" });
  await expect(second).toContainText(`${t} Carrier implements ${t} Flow`);
  await second.getByLabel("Confirmed by (required)").selectOption({ label: "Dana Ortiz" });
  await second.getByLabel("Evidence / reference (required)").fill("Ticket 42");
  await second.getByRole("button", { name: "Confirm link" }).click();
  await expect(impl.getByText("✓Confirmed", { exact: true })).toBeVisible();
});

test("Viewer can read link state and history but not confirm", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const a = await post(editor, "applications", { name: `${t} A`, lifecycle: "active" });
  const b = await post(editor, "applications", { name: `${t} B`, lifecycle: "active" });
  const rel = await post(editor, "relationships", { type: "depends_on", source: a.id, target: b.id });
  await post(editor, `relationships/${rel.id}/confirmations`, { confirmed_by: await personId(editor, "Lee Chen"), evidence: "Seen in the runbook" });
  const flow = await post(editor, "integrations", { name: `${t} Flow`, lifecycle: "active", source: a.id, target: b.id });

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/applications/${a.id}?tab=relationships`);
  const row = viewer.getByRole("region", { name: "Depends on table" }).getByRole("row", { name: new RegExp(`${t} B`) });
  await expect(row.getByText("✓Confirmed", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: /^Confirm link/ })).toHaveCount(0);
  await row.getByRole("button", { name: /^History/ }).click();
  await expect(viewer.getByRole("dialog", { name: "Confirmation history" }).getByText("Seen in the runbook")).toBeVisible();
  await viewer.keyboard.press("Escape");

  await viewer.goto(`/integrations/${flow.id}`);
  await expect(viewer.getByRole("button", { name: "Confirm link", exact: true })).toHaveCount(0);
  await viewer.getByRole("tab", { name: "Confirmation history" }).click();
  await expect(viewer.getByText("Never confirmed", { exact: true })).toBeVisible();
});
