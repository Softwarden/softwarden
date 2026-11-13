import { expect, test } from "@playwright/test";
import { expectNoViolations } from "./axe";
import { pageAs, post, token } from "./helpers";

/** AI providers and models (CRUD, delete refused while in use), the Models in use report, and software
 *  categories. Everyone reads; Editors and Admins write. */

test("AI providers and models: add, edit, delete; a used one can't be deleted; the 409 is shown", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();

  // Providers: add one through the page.
  await page.goto("/ai-providers");
  await expect(page.getByRole("heading", { level: 1, name: "AI providers" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "AI models sections" }).getByRole("link", { name: "Providers" })).toHaveAttribute("aria-current", "page");
  await page.getByRole("button", { name: "Add provider" }).first().click();
  let dialog = page.getByRole("dialog", { name: "Add AI provider" });
  await dialog.getByRole("button", { name: "Add provider" }).click();
  await expect(dialog.getByRole("alert")).toContainText("Enter a name.");
  await expectNoViolations(page, "Add AI provider with an error");
  await dialog.getByLabel("Name (required)").fill(`${t} Labs`);
  await dialog.getByLabel("Website").fill("https://labs.example");
  await dialog.getByRole("button", { name: "Add provider" }).click();
  await expect(page.getByRole("status").filter({ hasText: `${t} Labs added.` })).toBeAttached();
  const providers = page.getByRole("region", { name: "AI providers table" });
  await expect(providers.getByRole("row", { name: new RegExp(`${t} Labs`) })).toContainText("No models");
  await expectNoViolations(page, "AI providers");

  // Models: add one, then fix its version.
  await page.getByRole("navigation", { name: "AI models sections" }).getByRole("link", { name: "Models", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: "AI models" })).toBeVisible();
  await page.getByRole("button", { name: "Add model" }).first().click();
  dialog = page.getByRole("dialog", { name: "Add AI model" });
  await dialog.getByLabel("Provider (required)").selectOption({ label: `${t} Labs` });
  await dialog.getByLabel("Name (required)").fill(`${t} Sonnet`);
  await dialog.getByLabel("Version").fill("4");
  await dialog.getByLabel("Modality").selectOption({ label: "Text" });
  await dialog.getByLabel("Hosting region").fill("EU");
  await dialog.getByRole("button", { name: "Add model" }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByLabel("Search AI models").fill(t);
  await expect(page).toHaveURL(new RegExp(`q=${t}`));
  const modelsTable = page.getByRole("region", { name: "AI models table" });
  const row = modelsTable.getByRole("row", { name: new RegExp(`${t} Sonnet`) });
  await expect(row).toContainText("EU");
  await row.getByRole("button", { name: `Edit ${t} Labs ${t} Sonnet 4` }).click();
  dialog = page.getByRole("dialog", { name: `Edit ${t} Labs ${t} Sonnet 4` });
  await dialog.getByLabel("Version").fill("4.5");
  await expectNoViolations(page, "Edit AI model");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(row).toContainText("4.5");
  await expect(row).toContainText("No agents");

  // A model an agent uses can't be deleted: the dialog says why and links to the agents.
  const models = await (await page.request.get(`/api/v1/ai-models?q=${t}`)).json();
  const sonnet = models.find((m: { name: string }) => m.name === `${t} Sonnet`);
  const agent = await post(page, "ai-agents", { name: `${t} Agent`, lifecycle: "active", models_used: [sonnet.id] });
  await page.reload();
  await expect(row).toContainText("1 agent");
  await expect(row.getByRole("link", { name: /1 agent/ })).toHaveAttribute("href", `/ai-agents?model=${sonnet.id}&archived=all`);
  await row.getByRole("button", { name: `Delete ${t} Labs ${t} Sonnet 4.5` }).click();
  dialog = page.getByRole("dialog", { name: `Delete ${t} Labs ${t} Sonnet 4.5?` });
  await expect(dialog.getByRole("alert")).toContainText("1 agent uses it");
  await expect(dialog.getByRole("button", { name: /^Delete / })).toHaveCount(0);
  await expect(dialog.getByRole("link", { name: /Show the agents that use/ })).toHaveAttribute("href", `/ai-agents?model=${sonnet.id}&archived=all`);
  await expectNoViolations(page, "Delete refused: model in use");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(row.getByRole("button", { name: `Delete ${t} Labs ${t} Sonnet 4.5` })).toBeFocused();

  // A model that became used after the page loaded: the API's 409 is shown in the dialog.
  const spare = await post(page, "ai-models", { provider: sonnet.provider.id, name: `${t} Haiku` });
  await page.reload();
  const spareRow = modelsTable.getByRole("row", { name: new RegExp(`${t} Haiku`) });
  await expect(spareRow).toContainText("No agents");
  await post(page, `ai-agents/${agent.id}`, { models_used: [sonnet.id, spare.id] }, "PATCH");
  await spareRow.getByRole("button", { name: `Delete ${t} Labs ${t} Haiku` }).click();
  dialog = page.getByRole("dialog", { name: `Delete ${t} Labs ${t} Haiku?` });
  await dialog.getByRole("button", { name: `Delete ${t} Labs ${t} Haiku` }).click();
  await expect(dialog.getByRole("alert")).toContainText("can't be deleted");
  await expect(dialog.getByRole("alert")).toContainText(`${t} Agent`);
  await expectNoViolations(page, "Delete refused by the API (409)");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // An unused model deletes.
  await post(page, `ai-agents/${agent.id}`, { models_used: [sonnet.id] }, "PATCH");
  await page.reload();
  await spareRow.getByRole("button", { name: `Delete ${t} Labs ${t} Haiku` }).click();
  dialog = page.getByRole("dialog", { name: `Delete ${t} Labs ${t} Haiku?` });
  await dialog.getByRole("button", { name: `Delete ${t} Labs ${t} Haiku` }).click();
  await expect(spareRow).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "deleted" })).toBeAttached();

  // The provider has a model, so it can't be deleted either.
  await page.goto("/ai-providers");
  const providerRow = providers.getByRole("row", { name: new RegExp(`${t} Labs`) });
  await expect(providerRow.getByRole("link", { name: /1 model/ })).toHaveAttribute("href", `/ai-models?provider=${sonnet.provider.id}`);
  await providerRow.getByRole("button", { name: `Delete ${t} Labs` }).click();
  dialog = page.getByRole("dialog", { name: `Delete ${t} Labs?` });
  await expect(dialog.getByRole("alert")).toContainText("1 model belongs to it");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // Filters are in the URL.
  await page.goto(`/ai-models?provider=${sonnet.provider.id}&in_use=true`);
  await expect(page.getByLabel("Provider", { exact: true })).toHaveValue(sonnet.provider.id);
  await expect(page.getByLabel("Use", { exact: true })).toHaveValue("true");
  await expect(modelsTable.getByRole("row", { name: new RegExp(`${t} Sonnet`) })).toBeVisible();
  await page.goto(`/ai-models?provider=${sonnet.provider.id}&in_use=false`);
  await expect(page.getByText("No AI models match these filters")).toBeVisible();

  // Viewers read and can't write.
  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto(`/ai-models?q=${t}`);
  await expect(viewer.getByRole("region", { name: "AI models table" }).getByRole("row", { name: new RegExp(`${t} Sonnet`) })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Add model" })).toHaveCount(0);
  await expect(viewer.getByRole("button", { name: /^Edit / })).toHaveCount(0);
  await expect(viewer.getByText("Editors and Admins can add, change and delete them.")).toBeVisible();
  await expectNoViolations(viewer, "AI models as a Viewer");
});

test("Models in use: counts agents per model, links to them; empty and error states", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  const provider = await post(page, "ai-providers", { name: `${t} Cloud` });
  const model = await post(page, "ai-models", { provider: provider.id, name: `${t} Turbo`, version: "3" });
  await post(page, "ai-agents", { name: `${t} One`, lifecycle: "active", models_used: [model.id] });
  await post(page, "ai-agents", { name: `${t} Two`, lifecycle: "proposed", models_used: [model.id] });

  await page.goto("/ai-models/in-use");
  await expect(page.getByRole("heading", { level: 1, name: "Models in use" })).toBeVisible();
  const table = page.getByRole("region", { name: "Models in use table" });
  const row = table.getByRole("row", { name: new RegExp(`${t} Turbo`) });
  await expect(row.getByRole("cell").nth(3)).toHaveText(/^2/);
  await expect(row.getByRole("cell").nth(4)).toHaveText("1"); // only the Active one is live
  await expect(row.getByRole("link")).toHaveAttribute("href", `/ai-agents?model=${model.id}`);
  await expect(table.getByRole("columnheader", { name: "Agents", exact: true })).toHaveAttribute("aria-sort", "descending");
  await expectNoViolations(page, "Models in use");
  await row.getByRole("link").click();
  await expect(page).toHaveURL(`/ai-agents?model=${model.id}`);
  await expect(page.getByRole("region", { name: "AI agents table" }).getByRole("link", { name: `${t} One` })).toBeVisible();

  await page.route("**/api/v1/ai-models/in-use", (route) => route.fulfill({ json: [] }));
  await page.goto("/ai-models/in-use");
  await expect(page.getByText("No agent uses a model yet")).toBeVisible();
  await expectNoViolations(page, "Models in use, empty");
  await page.unroute("**/api/v1/ai-models/in-use");
  await page.route("**/api/v1/ai-models/in-use", (route) => route.fulfill({ status: 500, json: { detail: "The report failed." } }));
  await page.goto("/ai-models/in-use");
  await expect(page.getByRole("alert")).toContainText("The report failed.");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("Software categories: add, edit, delete; one in use can't be deleted; Viewers read", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  const t = token();
  await page.goto("/categories");
  await expect(page.getByRole("heading", { level: 1, name: "Software categories" })).toBeVisible();
  const table = page.getByRole("region", { name: "Software categories table" });
  // The default list is seeded for every organization.
  await expect(table.getByRole("row")).not.toHaveCount(1);

  await page.getByRole("button", { name: "Add category" }).first().click();
  let dialog = page.getByRole("dialog", { name: "Add software category" });
  await dialog.getByLabel("Name (required)").fill(`${t} Robotics`);
  await dialog.getByLabel("Description").fill("Robots and drones");
  await dialog.getByRole("button", { name: "Add category" }).click();
  const row = table.getByRole("row", { name: new RegExp(`${t} Robotics`) });
  await expect(row).toContainText("Robots and drones");
  // A duplicate name is a field error, inline.
  await page.getByRole("button", { name: "Add category" }).first().click();
  dialog = page.getByRole("dialog", { name: "Add software category" });
  await dialog.getByLabel("Name (required)").fill(`${t} robotics`);
  await dialog.getByRole("button", { name: "Add category" }).click();
  await expect(dialog.getByLabel("Name (required)")).toHaveAttribute("aria-invalid", "true");
  await expectNoViolations(page, "Add category with a duplicate name");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await row.getByRole("button", { name: `Edit ${t} Robotics` }).click();
  dialog = page.getByRole("dialog", { name: `Edit ${t} Robotics` });
  await dialog.getByLabel("Description").fill("Robots, drones and arms");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(row).toContainText("Robots, drones and arms");

  // In use by an application: the delete is refused with a link to the applications.
  const categories = await (await page.request.get("/api/v1/categories")).json();
  const robotics = categories.find((c: { name: string }) => c.name === `${t} Robotics`);
  await post(page, "applications", { name: `${t} Arm Control`, lifecycle: "active", category: robotics.id });
  await page.reload();
  await expect(row.getByRole("link", { name: /1 application/ })).toHaveAttribute("href", `/applications?category=${robotics.id}&archived=all`);
  await row.getByRole("button", { name: `Delete ${t} Robotics` }).click();
  dialog = page.getByRole("dialog", { name: `Delete ${t} Robotics?` });
  await expect(dialog.getByRole("alert")).toContainText("1 application is in this category");
  await expectNoViolations(page, "Delete category refused");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // An unused one deletes.
  await post(page, "categories", { name: `${t} Spare` });
  await page.reload();
  const spare = table.getByRole("row", { name: new RegExp(`${t} Spare`) });
  await spare.getByRole("button", { name: `Delete ${t} Spare` }).click();
  await page.getByRole("dialog", { name: `Delete ${t} Spare?` }).getByRole("button", { name: `Delete ${t} Spare` }).click();
  await expect(spare).toHaveCount(0);

  const viewer = await pageAs(browser, baseURL, "viewer@example.test");
  await viewer.goto("/categories");
  await expect(viewer.getByRole("region", { name: "Software categories table" }).getByRole("row", { name: new RegExp(`${t} Robotics`) })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Add category" })).toHaveCount(0);
  await expectNoViolations(viewer, "Software categories as a Viewer");

  await viewer.route("**/api/v1/categories", (route) => route.fulfill({ status: 500, json: { detail: "Categories failed." } }));
  await viewer.goto("/categories");
  await expect(viewer.getByRole("alert")).toContainText("Categories failed.");
});

test("A reference page whose code can't load says so and offers a reload; the rest of the app keeps working", async ({ browser, baseURL }) => {
  const page = await pageAs(browser, baseURL, "editor@example.test");
  // The lazily loaded chunk (in the dev server, the module itself) fails to arrive.
  await page.route(/\/src\/features\/catalog\/reference\.tsx/, (route) => route.abort());
  await page.goto("/ai-models");
  const alert = page.getByRole("alert").filter({ hasText: "Couldn't load this page. Reload the page to try again." });
  await expect(alert).toBeVisible();
  await expect(alert.getByRole("button", { name: "Reload the page" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible(); // the shell is still there
  await expectNoViolations(page, "Reference page unavailable");
  await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Applications" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Applications" })).toBeVisible();
});
