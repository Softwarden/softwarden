"""A TypeScript client generated from the OpenAPI schema compiles.

The schema is generated here, `openapi-typescript` (apps/web devDependency) turns it into types in a temporary folder,
and `tsc --noEmit --strict` compiles them with a small file that uses them the way the SPA does: a record, a paginated
list, a request body, an enum and the Problem shape. The check runs in the API gate because only an API change can
break it; it needs apps/web's node_modules (`npm install` there) and is skipped, with the reason, when they are
missing (and fails when the CI variable is set, so CI can't skip it). docs/API.md "Generated client" says how to emit the same types into the SPA."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from django.core.management import call_command

WEB = Path(__file__).resolve().parents[3] / "apps" / "web"
GENERATOR = WEB / "node_modules" / "openapi-typescript" / "bin" / "cli.js"
TSC = WEB / "node_modules" / "typescript" / "bin" / "tsc"

USES = """\
import type { components, operations, paths } from "./openapi";

type Schemas = components["schemas"];
type Problem = Schemas["Problem"];
type Application = paths["/api/v1/applications/{id}"]["get"]["responses"][200]["content"]["application/json"];
type Page = paths["/api/v1/applications"]["get"]["responses"][200]["content"]["application/json"];
type NewApplication = NonNullable<paths["/api/v1/applications"]["post"]["requestBody"]>["content"]["application/json"];
type Refused = paths["/api/v1/applications/{id}"]["patch"]["responses"][412]["content"]["application/problem+json"];
type Limited = operations["applications_list"]["responses"][429]["content"]["application/problem+json"];

const lifecycle: Schemas["LifecycleEnum"] = "active";
const body: NewApplication = { name: "Payroll", lifecycle };
const problem: Problem = { type: "/problems/precondition-failed", title: "Precondition failed", status: 412, detail: "" };
const refused: Refused = { ...problem, revision: "0" };
const limited: Limited = { ...problem, type: "/problems/rate-limited", status: 429 };

export function first(page: Page): Application["name"] | undefined {
  return page.results[0]?.name;
}
export const sample = { body, refused, limited };
"""

TSCONFIG = """\
{
  "compilerOptions": {
    "strict": true, "noEmit": true, "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler",
    "noUncheckedIndexedAccess": true, "exactOptionalPropertyTypes": true, "skipLibCheck": false, "types": []
  },
  "files": ["openapi.d.ts", "uses.ts"]
}
"""


def run(*args: str) -> subprocess.CompletedProcess:
    node = shutil.which("node")
    assert node is not None
    return subprocess.run([node, *args], capture_output=True, text=True, timeout=300, check=False)  # noqa: S603


def test_a_generated_typescript_client_compiles(tmp_path):
    if shutil.which("node") is None or not GENERATOR.exists() or not TSC.exists():
        reason = "needs Node and apps/web/node_modules (cd apps/web && npm install)"
        if os.environ.get("CI"):
            pytest.fail(f"The generated-client check can't run in CI: {reason}.")
        pytest.skip(reason)
    schema = tmp_path / "openapi.json"
    call_command("spectacular", "--format", "openapi-json", "--validate", "--fail-on-warn", "--file", str(schema))
    generated = run(str(GENERATOR), str(schema), "--output", str(tmp_path / "openapi.d.ts"))
    assert generated.returncode == 0, generated.stderr
    (tmp_path / "uses.ts").write_text(USES, encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(TSCONFIG, encoding="utf-8")
    compiled = run(str(TSC), "-p", str(tmp_path / "tsconfig.json"))
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
