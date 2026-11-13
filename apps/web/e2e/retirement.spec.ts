import { expect, test, type Page } from "@playwright/test";
import { pageAs, post, token } from "./helpers";

/** Retire a record with active dependents from the edit form: the acknowledgment dialog (the one on the assessment status menu is in assessments.spec.ts). */

const app = (page: Page, name: string, extra: object = {}) => post(page, "applications", { name, lifecycle: "active", ...extra });
const component = (page: Page, name: string, extra: object = {}) => post(page, "components", { name, lifecycle: "active", component_type: "script", ...extra });
const dependsOn = (page: Page, source: string, target: string) => post(page, "relationships", { type: "depends_on", source, target });

test("Retire a record with active dependents: acknowledgment dialog from the edit form", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const core = await app(editor, `${t} Legacy`);
  const kiosk = await app(editor, `${t} Kiosk`);
  const badge = await component(editor, `${t} Badge`);
  const old = await app(editor, `${t} Reports`, { lifecycle: "retired" });
  const shelved = await app(editor, `${t} Shelved`);
  for (const d of [kiosk, badge, old, shelved]) await dependsOn(editor, d.id, core.id);
  await post(editor, `applications/${shelved.id}/archive`, {});

  await editor.goto(`/applications/${core.id}/edit`);
  await editor.getByLabel("Lifecycle (required)").selectOption("retired");
  const save = editor.getByRole("button", { name: "Save" });
  await save.click();
  const dialog = editor.getByRole("dialog", { name: `Retire ${t} Legacy?` });
  await expect(dialog).toBeVisible();
  const dependents = dialog.getByRole("list", { name: "Active dependents" });
  await expect(dependents.getByRole("listitem")).toHaveCount(2);
  await expect(dependents).toContainText(`${t} Kiosk`);
  await expect(dependents).toContainText(`${t} Badge`);
  await expect(dialog).toContainText(`${t} Reports (Retired)`);
  await expect(dialog).toContainText(`${t} Shelved (Archived)`);
  await expect(dialog).toContainText("don't need acknowledgment");
  await expect(dialog).toContainText("Retiring doesn't remove dependency evidence");

  const confirm = dialog.getByRole("button", { name: "Retire with acknowledgment" });
  await expect(confirm).toHaveAttribute("aria-disabled", "true");
  await expect(dialog.getByText("Enter a reason and tick the box to continue.")).toBeVisible();
  await expect(dialog.getByLabel(/^Reason \(required/)).toBeFocused();
  await expect(confirm).toBeDisabled(); // aria-disabled, so Playwright won't click it; its handler must ignore a click
  const writes: string[] = [];
  const onRequest = (r: { method(): string; url(): string }) => {
    if (r.method() !== "GET" && r.url().includes("/api/v1/")) writes.push(`${r.method()} ${r.url()}`);
  };
  editor.on("request", onRequest);
  await confirm.dispatchEvent("click"); // does nothing while disabled
  await expect(dialog).toBeVisible();
  await editor.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  editor.off("request", onRequest);
  expect(writes).toEqual([]); // no retire (or any other write) was sent
  await dialog.getByLabel(/^Reason \(required/).fill("Kiosk moves to the new vendor API on 2026-12-01");
  await expect(confirm).toHaveAttribute("aria-disabled", "true");
  await expect(dialog.getByText("Tick the box to continue.")).toBeVisible();
  await dialog.getByRole("checkbox", { name: "I acknowledge 2 active dependents" }).check();
  await expect(confirm).toHaveAttribute("aria-disabled", "false");

  // Cancel returns focus to Save and changes nothing.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(save).toBeFocused();
  await expect((await editor.request.get(`/api/v1/applications/${core.id}`)).json()).resolves.toMatchObject({ lifecycle: "active" });

  await save.click();
  await editor.getByRole("dialog").getByLabel(/^Reason \(required/).fill("Kiosk moves to the new vendor API on 2026-12-01");
  await editor.getByRole("dialog").getByRole("checkbox").check();
  await editor.getByRole("dialog").getByRole("button", { name: "Retire with acknowledgment" }).click();
  await expect(editor).toHaveURL(new RegExp(`/applications/${core.id}$`));
  await expect(editor.getByText(/Retired with acknowledgment of 2 active dependents\. Reason saved to the audit history: Kiosk moves to the new vendor API on 2026-12-01/).first()).toBeVisible();
  const saved = await (await editor.request.get(`/api/v1/applications/${core.id}`)).json();
  expect(saved.lifecycle).toBe("retired");
  const links = await (await editor.request.get(`/api/v1/relationships?resource=${core.id}`)).json();
  expect(links).toHaveLength(4); // links are kept
});

test("Retire a record with no active dependents needs no acknowledgment", async ({ browser, baseURL }) => {
  const t = token();
  const editor = await pageAs(browser, baseURL, "editor@example.test");
  const solo = await app(editor, `${t} Solo`);
  await editor.goto(`/applications/${solo.id}/edit`);
  await editor.getByLabel("Lifecycle (required)").selectOption("retired");
  await editor.getByRole("button", { name: "Save" }).click();
  await expect(editor).toHaveURL(new RegExp(`/applications/${solo.id}$`));
  await expect(editor.getByRole("dialog")).toHaveCount(0);
});
