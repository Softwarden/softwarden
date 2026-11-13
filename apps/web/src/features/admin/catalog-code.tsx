/** Catalog as code (`/admin/catalog-code`, Admin only, capability `catalog_code`; loaded on first use):
 *  export the catalog as a zip of YAML files (GET catalog-code/export.zip, refused while platform staff impersonate, so
 *  the button is replaced by a sentence then), and upload a tree to see what applying it would do (POST
 *  catalog-code/plan: creates, updates, deletes with prune, unchanged, file errors and warnings), then apply that plan
 *  (POST catalog-code/apply with its plan_hash: 409 when the catalog or the files changed since, 400 with file_errors).
 *  The API takes a zip; YAML files chosen on their own are put in an uncompressed zip here first (a file's name doesn't
 *  matter, its content says what it is: docs/CATALOG_AS_CODE.md). Field changes reuse DiffView. */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Problem, problemFrom, readAnswer, writeHeaders } from "@/api";
import { AdminOnly, ErrorBanner, PageHead, TableRegion } from "@/components/app";
import { DiffView, type DiffRow } from "@/components/diff-view";
import { chip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDownload } from "@/lib/download";
import { Input } from "@/components/ui/input";
import type { Schemas } from "@/lib/openapi";
import { useInvalidate } from "@/lib/query";
import { useMe } from "@/lib/session";

type Plan = Schemas["Plan"];
type Issue = Schemas["Issue"];
type Change = Schemas["Change"];

const WHAT = "the catalog's export as YAML files and the plan and apply of a changed tree";

export function CatalogCodePage() {
  return <AdminOnly title="Catalog as code" what={WHAT}><CatalogCode /></AdminOnly>;
}

// ---- a stored (uncompressed) zip, for YAML files chosen on their own -----------------------------------------------------

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(data: Uint8Array) {
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++) c = CRC_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** Files into one zip, stored without compression (names in UTF-8, the date 1980-01-01). */
async function zipFiles(files: { name: string; data: Uint8Array }[]): Promise<Blob> {
  const enc = new TextEncoder();
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const f of files) {
    const name = enc.encode(f.name);
    const crc = crc32(f.data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true); local.setUint16(4, 20, true); local.setUint16(6, 0x0800, true);
    local.setUint16(8, 0, true); local.setUint16(10, 0, true); local.setUint16(12, 0x21, true);
    local.setUint32(14, crc, true); local.setUint32(18, f.data.length, true); local.setUint32(22, f.data.length, true);
    local.setUint16(26, name.length, true); local.setUint16(28, 0, true);
    const dir = new DataView(new ArrayBuffer(46));
    dir.setUint32(0, 0x02014b50, true); dir.setUint16(4, 20, true); dir.setUint16(6, 20, true); dir.setUint16(8, 0x0800, true);
    dir.setUint16(10, 0, true); dir.setUint16(12, 0, true); dir.setUint16(14, 0x21, true);
    dir.setUint32(16, crc, true); dir.setUint32(20, f.data.length, true); dir.setUint32(24, f.data.length, true);
    dir.setUint16(28, name.length, true); dir.setUint32(42, offset, true);
    parts.push(new Uint8Array(local.buffer), name, f.data);
    central.push(new Uint8Array(dir.buffer), name);
    offset += 30 + name.length + f.data.length;
  }
  const size = central.reduce((n, p) => n + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true); end.setUint16(10, files.length, true);
  end.setUint32(12, size, true); end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, new Uint8Array(end.buffer)] as BlobPart[], { type: "application/zip" });
}

/** What to upload: a zip as it is, or the YAML files chosen put into one zip. */
async function treeOf(files: File[]): Promise<{ blob: Blob; name: string }> {
  if (files.length === 1 && /\.zip$/i.test(files[0].name)) return { blob: files[0], name: files[0].name };
  const used = new Set<string>();
  const entries = await Promise.all(files.map(async (f, i) => {
    const name = used.has(f.name) ? `${i}/${f.name}` : f.name;
    used.add(name);
    return { name, data: new Uint8Array(await f.arrayBuffer()) };
  }));
  return { blob: await zipFiles(entries), name: "catalog.zip" };
}

async function send(path: "plan" | "apply", tree: { blob: Blob; name: string }, prune: boolean, allowMassDelete: boolean, planHash?: string): Promise<Plan> {
  const form = new FormData();
  form.append("file", tree.blob, tree.name);
  form.append("prune", prune ? "true" : "false");
  // A prune past the cap is a plan error unless this is sent, on the plan and the apply alike.
  if (prune && allowMassDelete) form.append("allow_mass_delete", "true");
  if (planHash) form.append("plan_hash", planHash);
  let res: Response;
  try {
    res = await fetch(`/api/v1/catalog-code/${path}`, { method: "POST", body: form, credentials: "same-origin", headers: { Accept: "application/json", ...writeHeaders() } });
  } catch {
    throw new Problem(0, "The upload didn't reach Softwarden. Check your connection and try again.");
  }
  // A 2xx that isn't JSON (a proxy's page) is the unreadable-response Problem, never an empty plan.
  const data = await readAnswer(res, "POST");
  if (!res.ok) throw problemFrom(res.status, data, Number(res.headers.get("Retry-After")), res.status === 413 ? "The upload is larger than this installation accepts." : "Something went wrong.");
  return data as Plan;
}

// ---- the page --------------------------------------------------------------------------------------------------------------

function CatalogCode() {
  const me = useMe();
  if (!me.capabilities.includes("catalog_code")) {
    return (
      <>
        <PageHead title="Catalog as code" />
        <p className="rounded-md border border-dashed border-border-strong bg-muted px-3 py-2" data-unavailable="">
          <span aria-hidden="true">⊘ </span><span className="font-semibold">Catalog as code isn't available.</span> It is part of every edition, and has been turned off for this organization: ask the installation's operator to turn it back on.
        </p>
      </>
    );
  }
  return (
    <>
      <PageHead title="Catalog as code" desc="The catalog as YAML files, one per record, for a git repository: export it, change the files, and see exactly what applying them would do before anything is saved." />
      <ExportSection />
      <PlanSection />
      <section aria-labelledby="cli-h" className="mb-6 max-w-3xl">
        <h2 id="cli-h" className="mb-2 font-display text-section font-semibold">From the command line</h2>
        <p className="mb-2 text-muted-foreground">For a first load of more than 2,000 records, or a CI pipeline, use the management commands on the server. They work on a folder rather than a zip.</p>
        <pre className="overflow-x-auto rounded-md border bg-muted px-3 py-2 text-sm" tabIndex={0} aria-label="Catalog as code commands">{`manage.py catalog_export --org <slug> --dir <folder> [--include-people]
manage.py catalog_apply --org <slug> --dir <folder> --dry-run [--prune] [--json]
manage.py catalog_apply --org <slug> --dir <folder> [--prune] --expect-hash <plan_hash>`}</pre>
      </section>
    </>
  );
}

function ExportSection() {
  const me = useMe();
  const [people, setPeople] = useState(false);
  const [notice, setNotice] = useState("");
  const exporter = useDownload();
  return (
    <section aria-labelledby="export-h" className="mb-6 max-w-3xl rounded-lg border bg-card px-4 py-4">
      <h2 id="export-h" className="mb-1 font-display text-section font-semibold">Export</h2>
      <p className="mb-3 text-muted-foreground">A zip of one YAML file per record. The same catalog always gives the same bytes, so a git diff shows only real changes. It never holds secrets, credentials, contracts, renewals or attachment files.</p>
      <SuccessNote text={notice} />
      {me.impersonation ? (
        <p className="text-sm" data-testid="export-impersonating"><span aria-hidden="true">⊘ </span>Exports are refused while platform staff act as a member, so there is nothing to download here now.</p>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex min-h-8 items-center gap-2 text-sm">
            <input type="checkbox" className="h-4 w-4 accent-primary" checked={people} onChange={(e) => setPeople(e.target.checked)} />
            Include people's names and emails (otherwise owners are named by their Softwarden ID)
          </label>
          <Button variant="outline" disabled={exporter.busy} onClick={async () => {
            setNotice("");
            const name = await exporter.download(`catalog-code/export.zip${people ? "?include_people=true" : ""}`, "softwarden-catalog.zip");
            if (name) setNotice(`Downloaded ${name}. The export is recorded in the audit log.`);
          }}>{exporter.busy ? "Exporting…" : "Download the catalog (zip)"}</Button>
        </div>
      )}
      {exporter.error && <div className="mt-3"><ErrorBanner message={exporter.error} /></div>}
    </section>
  );
}

const ACTION = {
  create: { glyph: "+", text: "Create", state: "confirmed" },
  update: { glyph: "◐", text: "Update", state: "pending" },
  delete: { glyph: "−", text: "Delete", state: "overdue" },
} as const;

function where(i: Issue) {
  return [i.file, i.line ? `line ${i.line}` : "", i.path].filter(Boolean).join(", ") || "The whole tree";
}

function Issues({ title, issues, tone, testId }: { title: string; issues: Issue[]; tone: "error" | "pending"; testId: string }) {
  if (!issues.length) return null;
  const box = tone === "error" ? "border-error-border bg-error-bg" : "border-state-pending-border bg-state-pending text-state-pending-foreground";
  return (
    <div className={`mb-3 rounded-md border border-l-4 px-4 py-3 ${box}`} data-testid={testId}>
      <p className="font-semibold"><span aria-hidden="true">{tone === "error" ? "! " : "◐ "}</span>{title} ({issues.length})</p>
      <ul className="ml-5 list-disc">
        {issues.map((i, n) => <li key={n} className="[overflow-wrap:anywhere]"><span className="font-semibold">{where(i)}:</span> {i.message}</li>)}
      </ul>
    </div>
  );
}

const fieldRows = (c: Change): DiffRow[] => c.fields.map((f) => ({
  field: f.path, label: f.path, before: f.before, after: f.after,
  change: f.before == null ? "added" : f.after == null ? "removed" : "changed",
}));

function Changes({ changes }: { changes: Change[] }) {
  if (!changes.length) return <p className="mb-3 text-muted-foreground">No record would change.</p>;
  return (
    <TableRegion label="Planned changes table" className="mb-3">
      <Table>
        <TableCaption className="sr-only">Planned changes, one row per record</TableCaption>
        <TableHeader><TableRow><TableHead scope="col">Record</TableHead><TableHead scope="col">Action</TableHead><TableHead scope="col">File</TableHead><TableHead scope="col">Fields</TableHead></TableRow></TableHeader>
        <TableBody>
          {changes.map((c, n) => {
            const a = ACTION[c.action];
            return (
              <TableRow key={`${c.kind}-${c.key}-${n}`} className="align-top" data-change={c.action}>
                <TableHead scope="row" className="max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                  {c.key}<span className="block text-sm font-normal text-muted-foreground">{c.kind}</span>
                </TableHead>
                <TableCell><span className={chip({ state: a.state })}><span aria-hidden="true">{a.glyph}</span>{a.text}</span></TableCell>
                <TableCell className="whitespace-normal text-sm [overflow-wrap:anywhere]">{c.file ? <code>{c.file}</code> : <span className="text-muted-foreground">No file (pruned)</span>}</TableCell>
                <TableCell className="min-w-64 whitespace-normal">
                  {c.fields.length === 0 ? <span className="text-muted-foreground">—</span> : (
                    <details>
                      <summary className="cursor-pointer">{c.fields.length} {c.fields.length === 1 ? "field" : "fields"}<span className="sr-only"> of {c.key}</span></summary>
                      <div className="mt-2">
                        <DiffView rows={fieldRows(c)} source="snapshots" mode={c.action === "create" ? "created" : c.action === "delete" ? "deleted" : "changed"} caption={`Fields of ${c.key}`} bare />
                      </div>
                    </details>
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

const MAX_BYTES = 16 * 1024 * 1024;
const isZip = (f: File) => /\.zip$/i.test(f.name);
const isYaml = (f: File) => /\.ya?ml$/i.test(f.name);

/** Why a selection can't be planned, before anything is read; "" when it can. */
function selectionProblem(files: File[]): string {
  if (!files.length) return "Choose a zip of the tree, or one or more YAML files.";
  const zips = files.filter(isZip).length;
  if (zips && files.length > 1) return "Choose one zip, or YAML files on their own, not both.";
  if (!zips && files.some((f) => !isYaml(f))) return "Choose a .zip, or files ending in .yaml or .yml.";
  if (files.reduce((n, f) => n + f.size, 0) > MAX_BYTES) return "The files are over 16 MB together. Use manage.py catalog_apply for a tree this large.";
  return "";
}

/** The query roots (the first path segment the SPA reads them under, checked against the screens' useApiQuery, useLoad,
 *  useTypedQuery and record-table paths) of everything an apply can change: the record lists, the reference data the
 *  tree holds, the reports and data quality the owners feed, and the Needs attention badge. */
const APPLIED_PATHS = ["applications", "components", "integrations", "ai-agents", "vendors", "people", "teams", "departments", "categories", "relationships",
  "relationship-types", "tags", "custom-fields", "ai-models", "ai-providers", "reports", "dependencies", "data-quality", "needs-attention", "search"];

function PlanSection() {
  const heading = useRef<HTMLHeadingElement>(null);
  const errorBox = useRef<HTMLDivElement>(null);
  const invalidate = useInvalidate();
  const [files, setFiles] = useState<File[]>([]);
  const [prune, setPrune] = useState(false);
  // The prune cap. The count the last plan refused (its error's `mass_delete`), and whether the Admin allows it.
  const [massDelete, setMassDelete] = useState<number | null>(null);
  const [allowMass, setAllowMass] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planned, setPlanned] = useState<{ blob: Blob; name: string; allowMass: boolean } | null>(null);
  const [checkedDeletes, setCheckedDeletes] = useState(false);
  const [busy, setBusy] = useState<"" | "plan" | "apply">("");
  const [error, setError] = useState("");
  const [fileError, setFileError] = useState("");
  const [applyIssues, setApplyIssues] = useState<Issue[]>([]);
  const [stale, setStale] = useState(false);
  const [applied, setApplied] = useState("");
  const looseYaml = files.length > 0 && !files.some(isZip);

  const reset = () => { setPlan(null); setPlanned(null); setError(""); setApplyIssues([]); setStale(false); setCheckedDeletes(false); };
  const focusError = () => requestAnimationFrame(() => errorBox.current?.focus());
  // Focus the file input once it is enabled again (it is disabled while a plan runs), so its error is read with it.
  const [focusFiles, setFocusFiles] = useState(0);
  useEffect(() => { if (focusFiles && !busy) document.getElementById("tree-files")?.focus(); }, [focusFiles, busy]);
  async function makePlan(e?: React.FormEvent) {
    e?.preventDefault();
    if (busy) return;
    setApplied("");
    const problem = selectionProblem(files);
    if (problem) { setFileError(problem); setFocusFiles((n) => n + 1); return; }
    setFileError(""); reset(); setBusy("plan");
    try {
      const tree = await treeOf(files);
      const pruning = prune && !looseYaml;
      const allowing = pruning && allowMass;
      const p = await send("plan", tree, pruning, allowing);
      const refused = p.errors.find((i) => typeof i.mass_delete === "number")?.mass_delete;
      if (refused !== undefined) setMassDelete(refused);
      setPlan(p); setPlanned({ ...tree, allowMass: allowing });
      requestAnimationFrame(() => document.getElementById("plan-h")?.focus());
    } catch (err) {
      if (err instanceof Problem && err.errors.file) { setFileError(err.errors.file.join(" ")); setFocusFiles((n) => n + 1); }
      else { setError(err instanceof Problem ? (Object.values(err.errors).flat().join(" ") || err.detail) : "Couldn't plan the files. Try again."); focusError(); }
    } finally {
      setBusy("");
    }
  }
  async function apply() {
    if (!plan || !planned || busy) return;
    setBusy("apply"); setError(""); setApplyIssues([]); setStale(false);
    try {
      const r = await send("apply", planned, plan.prune, planned.allowMass, plan.plan_hash);
      const s = r.summary;
      reset();
      setApplied(`Applied: ${s.create} created, ${s.update} updated, ${s.delete} deleted, ${s.unchanged} unchanged. Each change is in the audit log, marked as catalog as code.`);
      void invalidate(...APPLIED_PATHS);
      requestAnimationFrame(() => heading.current?.focus());
    } catch (err) {
      if (err instanceof Problem && err.type === "/problems/catalog-code-stale") setStale(true);
      else if (err instanceof Problem && Array.isArray(err.body.file_errors)) { setApplyIssues(err.body.file_errors as Issue[]); setError(err.detail); }
      else setError(err instanceof Problem ? err.detail : "Couldn't apply the plan. Nothing was saved. Try again.");
      focusError();
    } finally {
      setBusy("");
    }
  }
  const s = plan?.summary;
  const changes = s ? s.create + s.update + s.delete : 0;
  const deletes = s?.delete ?? 0;
  const why = !s ? "" : s.errors > 0 ? "Fix the errors in the files, then plan again."
    : changes === 0 ? "Nothing would change, so there is nothing to apply."
      : deletes > 0 && !checkedDeletes ? `Tick the box to confirm you've checked the ${deletes} ${deletes === 1 ? "item" : "items"} this deletes.` : "";
  return (
    <section aria-labelledby="planform-h" className="mb-6 max-w-5xl rounded-lg border bg-card px-4 py-4">
      <h2 id="planform-h" ref={heading} tabIndex={-1} className="mb-1 font-display text-section font-semibold outline-none">Plan and apply</h2>
      <p className="mb-3 max-w-prose text-muted-foreground">Upload the tree as a zip (an export, or a repository's zip), or choose one or more YAML files. The plan tries every change and rolls it back, so its errors are the ones applying would meet. Nothing is saved until you apply.</p>
      <SuccessNote text={applied} />
      <form noValidate onSubmit={(e) => void makePlan(e)} className="grid gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tree-files">Catalog files (required)</Label>
          <Input id="tree-files" type="file" multiple accept=".zip,.yaml,.yml" disabled={!!busy} className="h-auto py-1.5"
            aria-describedby={`tree-files-help${fileError ? " tree-files-error" : ""}`} aria-invalid={fileError ? true : undefined}
            onChange={(e) => { setFiles(Array.from(e.target.files ?? [])); setFileError(""); setPrune(false); setAllowMass(false); setMassDelete(null); reset(); }} />
          <span id="tree-files-help" className="text-sm text-muted-foreground">One zip, or YAML files (.yaml or .yml), up to 16 MB. Other files in a zip are ignored.</span>
          {fileError && <span id="tree-files-error" className="text-sm text-error-foreground">{fileError}</span>}
        </div>
        <div>
          <label className="flex min-h-8 items-center gap-2 text-sm">
            <input type="checkbox" className="h-4 w-4 accent-primary" checked={prune && !looseYaml} disabled={!!busy || looseYaml}
              aria-describedby={looseYaml ? "prune-help prune-off" : "prune-help"} onChange={(e) => { setPrune(e.target.checked); setAllowMass(false); setMassDelete(null); reset(); }} />
            Also delete records that have no file (prune)
          </label>
          <p id="prune-help" className="ml-6 text-sm text-muted-foreground">Only for the kinds the tree holds (its catalog.yaml, else the kinds it has files for). People and tags are never deleted. A prune that deletes more than 10 records, more than 20% of a kind or every record of a kind is refused unless you allow it.</p>
          {looseYaml && <p id="prune-off" className="ml-6 text-sm text-muted-foreground">Off for YAML files chosen on their own: they aren't a whole tree, so every other record of their kinds would be deleted.</p>}
          {prune && !looseYaml && (massDelete !== null || allowMass) && (
            <div className="ml-6 mt-2 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" data-testid="mass-delete">
              <label className="flex min-h-8 items-center gap-2 text-sm font-medium">
                <input type="checkbox" className="h-4 w-4 accent-primary" checked={allowMass} disabled={!!busy} aria-describedby="mass-delete-help"
                  onChange={(e) => { setAllowMass(e.target.checked); if (!e.target.checked) reset(); }} />
                {massDelete !== null ? `Allow this prune to delete ${massDelete} ${massDelete === 1 ? "record" : "records"}` : "Allow a prune past the cap"}
              </label>
              <p id="mass-delete-help" className="text-sm">Check that the tree is complete first: a partial checkout, a sparse clone or a truncated zip deletes every record it is missing. Then plan again; the plan lists each record it deletes, and applying asks you to confirm them.</p>
            </div>
          )}
        </div>
        <div><Button type="submit" variant={plan ? "outline" : "default"} disabled={!!busy}>{busy === "plan" ? "Planning…" : plan ? "Plan again" : "Preview the plan"}</Button></div>
      </form>
      <div ref={errorBox} tabIndex={-1} className="outline-none">
        {error && <div className="mt-3"><ErrorBanner message={error} /></div>}
        {stale && (
          <div className="mt-3 flex flex-wrap items-center gap-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert" data-testid="plan-stale">
            <strong>The catalog or the files changed since this plan was made, so nothing was applied. Plan again and review the new plan.</strong>
            <Button size="sm" variant="outline" onClick={() => void makePlan()}>Plan again</Button>
          </div>
        )}
        <Issues title="Problems that stopped the apply" issues={applyIssues} tone="error" testId="apply-errors" />
      </div>
      {plan && s && (
        <div className="mt-5 border-t pt-4" data-testid="plan">
          <h3 id="plan-h" tabIndex={-1} className="mb-2 font-display text-section font-semibold outline-none">The plan</h3>
          <p className="mb-3" role="status" data-testid="plan-summary">
            {s.files} {s.files === 1 ? "file" : "files"} read: {s.create} to create, {s.update} to update, {s.delete} to delete, {s.unchanged} unchanged, {s.errors} {s.errors === 1 ? "error" : "errors"}.
          </p>
          <Issues title="Errors to fix before applying" issues={plan.errors} tone="error" testId="plan-errors" />
          <Issues title="Left as they are" issues={plan.warnings} tone="pending" testId="plan-warnings" />
          <Changes changes={plan.changes} />
          {deletes > 0 && s.errors === 0 && (
            <label className="mb-3 flex min-h-8 items-center gap-2 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm">
              <input type="checkbox" className="h-4 w-4 accent-primary" checked={checkedDeletes} disabled={!!busy} onChange={(e) => setCheckedDeletes(e.target.checked)} />
              I've checked the {deletes} {deletes === 1 ? "item" : "items"} this deletes
            </label>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button variant={deletes > 0 ? "destructive" : "default"} disabled={!!why || !!busy} aria-describedby={why ? "apply-why" : undefined} onClick={() => void apply()}>
              {busy === "apply" ? "Applying…" : `Apply ${changes} ${changes === 1 ? "change" : "changes"}${deletes > 0 ? `, deleting ${deletes} ${deletes === 1 ? "item" : "items"}` : ""}`}
            </Button>
            {why && <p id="apply-why" className="text-sm text-muted-foreground">{why}</p>}
          </div>
        </div>
      )}
      <p className="mt-4 text-sm text-muted-foreground">The format is described in CATALOG_AS_CODE.md in the documentation. Applied changes are in the <Link to="/admin/audit" className="underline">audit log</Link>.</p>
    </section>
  );
}
