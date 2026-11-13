// TypeScript types for the API, generated from its OpenAPI schema (docs/API.md
// "Generated client"). `npm run api:types [-- out.d.ts]` asks the API for its schema (`uv run python manage.py
// spectacular` in apps/api: no server, no database) and writes the types with openapi-typescript, by default to
// src/generated/openapi.d.ts. apps/api/tests/test_typescript_client.py generates the same types into a temporary
// folder and compiles them in the API gate, so a schema change that breaks the client fails there.
//
// The SPA uses src/generated/openapi.d.ts (src/lib/openapi.ts), so the file is committed: a build never needs Python,
// and a schema change shows up in review as a diff of the types. `--check` (run by `npm run lint`) generates into a
// temporary file and fails when it differs from the committed one ("run npm run api:types"). Without `uv` on the PATH
// (the web-only CI job, a front-end-only machine) the check is skipped with that reason, unless SOFTWARDEN_API_TYPES_CHECK
// is "required", which makes a missing uv a failure too (set it where both toolchains are installed).
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const apiDir = path.resolve(web, "..", "api");
const args = process.argv.slice(2);
const check = args.includes("--check");
const committed = path.resolve(web, "src/generated/openapi.d.ts");
const work = mkdtempSync(path.join(tmpdir(), "softwarden-openapi-"));
const out = check ? path.join(work, "openapi.d.ts") : path.resolve(web, args.find((a) => !a.startsWith("--")) ?? "src/generated/openapi.d.ts");
const schema = path.join(work, "openapi.json");

function finish(code, message) {
  rmSync(work, { recursive: true, force: true });
  if (message) (code ? console.error : console.log)(message);
  process.exit(code);
}

function run(command, args, cwd, quiet = false) {
  // Development mode (off by default) lets manage.py load without a real secret key; the schema
  // doesn't depend on it.
  const env = { ...process.env, SOFTWARDEN_DEBUG: process.env.SOFTWARDEN_DEBUG ?? "1" };
  const done = spawnSync(command, args, { cwd, env, stdio: quiet ? ["ignore", "ignore", "inherit"] : "inherit", shell: false });
  if (done.error && done.error.code === "ENOENT") return "missing";
  if (done.status !== 0) finish(done.status ?? 1);
  return "ok";
}

const spectacular = ["run", "--quiet", "python", "manage.py", "spectacular", "--format", "openapi-json", "--validate", "--fail-on-warn", "--file", schema];
if (run("uv", spectacular, apiDir, check) === "missing") {
  if (check && process.env.SOFTWARDEN_API_TYPES_CHECK !== "required") {
    finish(0, "API types check skipped: uv isn't on the PATH, so the schema can't be generated here (apps/api's gate compiles a generated client instead).");
  }
  finish(1, "uv isn't on the PATH: install it (https://docs.astral.sh/uv/) to generate the API types.");
}
run(process.execPath, [path.join(web, "node_modules", "openapi-typescript", "bin", "cli.js"), schema, "--output", out], web, check);

if (check) {
  // Line endings can differ by checkout (core.autocrlf); the types can't.
  const normal = (file) => readFileSync(file, "utf8").replace(/\r\n/g, "\n");
  let current = "";
  try { current = normal(committed); } catch { finish(1, "src/generated/openapi.d.ts is missing: run `npm run api:types`."); }
  if (normal(out) !== current) finish(1, "src/generated/openapi.d.ts is out of date with the API's schema: run `npm run api:types` and commit it.");
  finish(0, "API types are current.");
}
finish(0, `API types written to ${path.relative(web, out)}`);
