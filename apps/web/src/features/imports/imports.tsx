/** CSV import: history, the one-run flow (upload, map columns, preview, commit, results) and the
 *  Admin's import sources. Nothing is written until Commit, and Commit is one transaction that the server refuses when a
 *  matched record changed after the preview. There is no undo.
 *
 *  Contracts and licence pools (the `contracts` capability). Their preview rows carry `computed` (a contract's
 *  annualised cost, cancellation deadline and how the cost splits over its lines; a pool's utilisation), a file over 500
 *  rows isn't rehearsed (`counts.rehearsed` 0 and a notice in `panels.notices`), and their links open the contract or
 *  pool pages. */
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, IMPORT_KIND_PLURAL, IMPORT_KINDS, type ImportKind, MONEY_IMPORT_KINDS, Problem, problemFrom, readAnswer, when, writeHeaders } from "@/api";
import { useDownload } from "@/lib/download";
import { currencyDecimals, groupDigits, percentText } from "@/features/money/contract-types";
import { useMe } from "@/lib/session";
import { RecordLink } from "@/components/record-link";
import { attempt, Breadcrumb, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, NotFound, PageHead, Select, TableRegion, useLoad, useReturnFocus } from "@/components/app";
import { chip, GapChip, GapChips, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ANY_REVISION, ifMatch, staleDeleteText, useOpenedRecord } from "@/lib/if-match";

// ---- types ----------------------------------------------------------------------------------------------------------

type Kind = ImportKind;
type RunStatus = "uploaded" | "previewed" | "committed" | "refused" | "cancelled";
type Outcome = "create" | "update" | "unchanged" | "error" | "excluded";
type Decision = "create" | "gap" | null;
type RunSummary = {
  id: string; kind: Kind; source: { id: string; name: string } | null; filename: string; status: RunStatus;
  uploaded_by: { id: number; name: string; email: string } | null; created_at: string; previewed_at: string | null;
  committed_at: string | null; refused_reason: string; purge_after: string; purged_at: string | null; purged: boolean;
  counts: Record<string, number>;
};
type Entry = { key: string; email: string; name: string; rows: number[]; fields: string[]; decision: Decision };
type MatchEntry = {
  row_number: number; name: string; candidate: { id: string; name: string; known_to_other_source: boolean }; decision: "create" | "match" | null;
};
type RunDetail = RunSummary & {
  decisions: Record<string, unknown>;
  can_commit: boolean;
  panels: { people: Entry[]; teams: Entry[]; vendors: Entry[]; matches: MatchEntry[]; matched: { people?: number; teams?: number; vendors?: number }; notices?: string[] };
  needs_mapping?: boolean;
  mapping?: Record<string, string> | null;
  headers?: string[];
  suggested_mapping?: Record<string, string>;
  fields?: { value: string; label: string; required: boolean }[];
};
type Source = { id: string; name: string; kind: Kind; identifiers: number; column_mapping: Record<string, unknown>; created_at: string };
type Msg = { column: string; message: string };
type Row = {
  row_number: number; name: string; outcome: Outcome; excluded: boolean; errors: Msg[]; warnings: Msg[]; gaps: string[]; needs_decision: Msg[];
  changes: Record<string, [unknown, unknown]>; needs_confirmation: boolean; matched: { id: string; name: string } | null;
  possible_match: { id: string; name: string } | null; result: { id: string; name: string; kind: string } | null;
  /** Contracts and licence pools: what the preview worked out; {} for records (and for rows a large file didn't rehearse). */
  computed?: Computed;
};
/** A contract row: its annualised cost, cancellation deadline and the split of its cost over its lines (a null record is
 *  the part on no line). A pool row: its seats and utilisation (over 100 is overuse). */
type Computed = {
  annualized_cost?: string | null; currency?: string; annualization_note?: string; cancellation_deadline?: string | null;
  deadline_label?: string | null; deadline_explanation?: string; renewal?: string | null;
  allocation?: { record: { id: string; name: string; kind: string } | null; share: string; amount: string | null }[];
  seats_purchased?: number; seats_assigned?: number; utilization?: number | null;
};
type RowPage = { count: number; results: Row[] };
type Refusal = { detail: string; changed: { row: number; name: string }[]; errorRows: number[] };

const REFUSED = "/problems/import-refused";
const OUTCOMES: { id: Outcome; label: string; glyph: string }[] = [
  { id: "create", label: "Create", glyph: "+" }, { id: "update", label: "Update", glyph: "↻" }, { id: "unchanged", label: "Unchanged", glyph: "=" },
  { id: "error", label: "Error", glyph: "✕" }, { id: "excluded", label: "Excluded", glyph: "–" },
];
const STATUS: Record<RunStatus, { label: string; glyph: string; state: "neutral" | "pending" | "confirmed" | "overdue" }> = {
  uploaded: { label: "Uploaded", glyph: "○", state: "neutral" }, previewed: { label: "Previewed", glyph: "◐", state: "pending" },
  committed: { label: "Committed", glyph: "✓", state: "confirmed" }, refused: { label: "Refused", glyph: "!", state: "overdue" },
  cancelled: { label: "Canceled", glyph: "⊘", state: "neutral" },
};
const FIELD_WORD: Record<string, string> = {
  business_owner: "business owner", technical_owner: "technical owner", support_team: "support team", it_steward: "IT steward", vendor: "vendor",
};

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const num = (c: Record<string, number>, k: string) => c[k] ?? 0;
const kindWord = (k: Kind, many = true) => (many ? IMPORT_KIND_PLURAL[k].toLowerCase() : k === "license_pool" ? "license pool" : k);
const isMoney = (k: Kind) => MONEY_IMPORT_KINDS.includes(k);
/** The kinds this organisation can import: contracts and licence pools need the `contracts` capability. */
const kindsFor = (capabilities: string[]) => IMPORT_KINDS.filter((k) => !isMoney(k) || capabilities.includes("contracts"));
/** What each kind needs per row, as the template's help says it. */
const MINIMUM: Record<Kind, string> = {
  application: "Minimum per row: name and lifecycle status. Owners can be blank and will show as gaps.",
  component: "Minimum per row: name and lifecycle status. Owners can be blank and will show as gaps.",
  contract: "Minimum per row: name. Lines go in items as “Microsoft 365=70; Entra ID=30”; a line without = shares the rest.",
  license_pool: "Minimum per row: application and sku_name. Seat sync needs a SKU ID.",
};
/** The page of a contract or licence pool a money row matched, created or updated. */
const moneyPath = (kind: string, id: string) => (kind === "contract" ? `/contracts/${id}` : kind === "license_pool" ? `/license-pools/${id}` : null);
const DEADLINE_LABEL: Record<string, string> = { entered: "Entered", calculated: "Calculated", assumed: "Assumed", renewal_action: "Renewal action" };
const day = (iso: string | null) => when(iso).slice(0, 10);
/** Words kept in capitals when a column name is put in words ("sku_id" is "SKU ID"). */
const ACRONYMS: Record<string, string> = { id: "ID", sku: "SKU", po: "PO", url: "URL", sso: "SSO", scim: "SCIM", it: "IT", cf: "CF" };
const label = (s: string) => {
  if (s.startsWith("cf_")) return `Custom field ${s.slice(3)}`;
  const t = s.split("_").map((w) => ACRONYMS[w] ?? w).join(" ");
  return t.charAt(0).toUpperCase() + t.slice(1);
};
/** "Column: " before a row message, except for custom fields, whose messages already start with the field's label. */
const columnPrefix = (column: string) => (!column || column.startsWith("cf_") ? "" : `${label(column)}: `);
const show = (v: unknown) => (v === null || v === undefined || v === "" ? "(blank)" : typeof v === "string" ? v : JSON.stringify(v));

/** Downloads (template, error report) through the shared helper; `run` keeps this page's call shape. */
function useFileDownload() {
  const d = useDownload();
  return { error: d.error, busy: d.busy, run: (path: string, fallback: string) => d.download(path, fallback) };
}

function StatusChip({ status }: { status: RunStatus }) {
  const s = STATUS[status];
  return <span className={chip({ state: s.state })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

function resultText(r: RunSummary) {
  if (r.status !== "committed") return "—";
  return `${num(r.counts, "created")} created · ${num(r.counts, "updated")} updated`;
}

function NoAccess({ title, who }: { title: string; who: string }) {
  return (
    <>
      <PageHead title={title} />
      <EmptyState title={`${title} are for ${who}`}>Your role can't use this page. Ask an Admin if you need access.</EmptyState>
    </>
  );
}

function Alert({ children, tone = "danger" }: { children: React.ReactNode; tone?: "danger" | "pending" }) {
  return (
    <div role="alert" className={`mb-4 rounded-md border border-l-4 px-4 py-3 ${tone === "danger" ? "border-error-border bg-error-bg" : "border-state-pending-border bg-state-pending"}`}>
      {children}
    </div>
  );
}

function Card({ id, title, note, children }: { id: string; title: string; note?: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id} className="mb-4 overflow-hidden rounded-lg border bg-card">
      <div className="flex flex-wrap items-baseline gap-x-3 border-b px-4 py-2.5">
        <h3 id={id} className="font-display text-section font-semibold">{title}</h3>
        {note && <span className="text-sm text-muted-foreground">{note}</span>}
      </div>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

/** Step indicator: an ordered list; the current step carries aria-current="step". */
function Steps({ steps, current }: { steps: string[]; current: number }) {
  return (
    <nav aria-label="Import progress" className="mb-5">
      <ol className="flex flex-wrap gap-x-5 gap-y-2">
        {steps.map((s, i) => (
          <li key={s} aria-current={i === current ? "step" : undefined} className={`flex items-center gap-2 text-sm ${i === current ? "font-semibold" : "text-muted-foreground"}`}>
            <span aria-hidden="true" className={`grid h-6 w-6 place-items-center rounded-full border text-xs font-semibold ${i === current ? "border-accent-line bg-primary text-primary-foreground" : i < current ? "border-success-border bg-success-bg text-success-foreground" : "border-border-strong"}`}>
              {i < current ? "✓" : i + 1}
            </span>
            <span>{s}{i < current && <span className="sr-only"> (done)</span>}</span>
          </li>
        ))}
      </ol>
    </nav>
  );
}

const stepsFor = (mapping: boolean) => ["Upload", ...(mapping ? ["Map columns"] : []), "Validate & match", "Preview", "Commit", "Results"];

function StepHeading({ children }: { children: React.ReactNode }) {
  return <h2 id="step-heading" tabIndex={-1} className="mb-3 font-display text-dialog font-semibold outline-none">{children}</h2>;
}

// ---- history --------------------------------------------------------------------------------------------------------

export function Imports() {
  const me = useMe();
  if (me.role === "viewer") return <NoAccess title="Imports" who="Editors and Admins" />;
  return <ImportHistory />;
}

function ImportHistory() {
  const me = useMe();
  const runs = useLoad<RunSummary[]>("imports");
  const start = <Button asChild><Link to="/imports/new">New import</Link></Button>;
  return (
    <>
      <PageHead title="Imports" desc={`Load ${me.capabilities.includes("contracts") ? "applications, components, contracts or license pools" : "applications or components"} from a CSV. You see exactly what will change before anything is saved.`}>
        {runs.data && runs.data.length > 0 && start}
      </PageHead>
      {runs.error && <ErrorBanner message={`Couldn't load imports. ${runs.error}`} onRetry={runs.reload} />}
      {!runs.data && !runs.error && <Loading what="imports" rows={4} />}
      {runs.data && runs.data.length === 0 && (
        <EmptyState title="No imports yet" action={start}>Upload a CSV with a name and lifecycle status for each row. Download the template on the next page to see the columns.</EmptyState>
      )}
      {runs.data && runs.data.length > 0 && (
        <TableRegion label="Imports table">
          <Table>
            <TableCaption className="sr-only">Import runs, newest first</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col" aria-sort="descending">Started</TableHead><TableHead scope="col">Kind</TableHead><TableHead scope="col">Source</TableHead>
              <TableHead scope="col">Result</TableHead><TableHead scope="col">By</TableHead><TableHead scope="col">Status</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {runs.data.map((r) => (
                <TableRow key={r.id} className="align-top">
                  <TableHead scope="row" className="whitespace-normal font-normal text-foreground">
                    <Link to={`/imports/${r.id}`} className="tabular">{when(r.created_at)}<span className="sr-only">, {r.filename}</span></Link>
                    <span className="block max-w-64 text-sm text-muted-foreground [overflow-wrap:anywhere]" aria-hidden="true">{r.filename}</span>
                  </TableHead>
                  <TableCell>{IMPORT_KIND_PLURAL[r.kind] ?? r.kind}</TableCell>
                  <TableCell>{r.source?.name ?? <span className="text-muted-foreground">None</span>}</TableCell>
                  <TableCell className="whitespace-normal">
                    {resultText(r)}
                    {r.purged && <span className="block text-sm text-muted-foreground">File deleted {day(r.purged_at)}</span>}
                  </TableCell>
                  <TableCell>{r.uploaded_by?.name ?? "—"}</TableCell>
                  <TableCell><StatusChip status={r.status} /></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
    </>
  );
}

// ---- one run --------------------------------------------------------------------------------------------------------

export function ImportRun() {
  const me = useMe();
  const { id = "" } = useParams();
  if (me.role === "viewer") return <NoAccess title="Imports" who="Editors and Admins" />;
  return id === "new" ? <UploadStep /> : <RunPage key={id} id={id} />;
}

function UploadStep() {
  const me = useMe();
  const navigate = useNavigate();
  const [kind, setKind] = useState<Kind>("application");
  const [source, setSource] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const sources = useLoad<Source[]>(`import-sources?kind=${kind}`, { strict: true });
  const template = useFileDownload();
  const kinds = kindsFor(me.capabilities);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setDetail("");
    if (!file) return setErrors({ file: ["Choose a CSV file."] });
    setErrors({});
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    if (source) form.append("source", source);
    setBusy(true);
    abort.current = new AbortController();
    try {
      const res = await fetch("/api/v1/imports", {
        method: "POST", body: form, credentials: "same-origin", headers: { Accept: "application/json", ...writeHeaders() }, signal: abort.current.signal,
      });
      // A 2xx that isn't JSON (a proxy's page) is the unreadable-response Problem, not a run without an ID.
      const data = await readAnswer(res, "POST");
      if (!res.ok) {
        // problemFrom: the limit's words, and the shell's handling of an impersonation that ended or started.
        const problem = problemFrom(res.status, data, Number(res.headers.get("Retry-After")), "Something went wrong. Try again.");
        if (Object.keys(problem.errors).length) setErrors(problem.errors);
        else setDetail(problem.detail);
        setBusy(false);
        return;
      }
      navigate(`/imports/${data.id}`, { state: { run: data } });
    } catch (err) {
      setBusy(false);
      if (err instanceof Problem) setDetail(err.detail);
      else if (!(err instanceof DOMException && err.name === "AbortError")) setDetail("Couldn't reach the server. Try again.");
    }
  }

  return (
    <>
      <Breadcrumb trail={[{ to: "/imports", label: "Imports" }]} current="New import" />
      <PageHead title="New import" desc={`Upload a CSV of ${kinds.map((k) => kindWord(k)).join(", ").replace(/, ([^,]*)$/, " or $1")}. Nothing is saved until you commit the preview.`} />
      <Steps steps={stepsFor(false)} current={0} />
      <StepHeading>Upload</StepHeading>
      {busy ? (
        <div className="max-w-2xl rounded-lg border bg-card p-4">
          <p role="status" className="mb-2">Checking {file?.name}… matching by Softwarden ID, then external key{kind === "contract" ? ", then contract number" : kind === "license_pool" ? ", then application and SKU name" : ""}.</p>
          <div role="progressbar" aria-label="Validating and matching" aria-valuetext={`Checking ${file?.name ?? "the file"}`} className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full w-1/3 rounded-full bg-primary motion-safe:animate-pulse" />
          </div>
          <Button type="button" variant="outline" className="mt-3" onClick={() => abort.current?.abort()}>Cancel</Button>
        </div>
      ) : (
        <form onSubmit={submit} noValidate className="max-w-2xl rounded-lg border bg-card p-4">
          <ErrorSummary errors={errors} focusOnShow />
          {detail && <Alert>{detail}</Alert>}
          <fieldset className="mb-4">
            <legend className="mb-1.5 text-sm font-medium">What are you importing?</legend>
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              {kinds.map((k) => (
                <label key={k} className="flex min-h-6 items-center gap-2">
                  <input type="radio" name="kind" value={k} checked={kind === k} className="h-4 w-4 accent-primary" onChange={() => { setKind(k); setSource(""); }} />
                  {IMPORT_KIND_PLURAL[k]}
                </label>
              ))}
            </div>
          </fieldset>
          <Field id="source" label="Import source" errors={errors} help="Optional. External keys from this source match records on re-import.">
            <div className="flex flex-wrap items-center gap-3">
              <Select id="source" className="max-w-xs" value={source} disabled={!sources.data} onChange={(e) => setSource(e.target.value)}>
                <option value="">{sources.data ? "No import source" : "Loading sources…"}</option>
                {(sources.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </Select>
              {me.role === "admin" && <Link to="/admin/import-sources" className="text-sm">New source</Link>}
            </div>
          </Field>
          {sources.error && <ErrorBanner message={`Couldn't load import sources. ${sources.error}`} onRetry={sources.reload} />}
          <Field id="file" label="CSV file" errors={errors}
            help={`UTF-8, up to 5 MB and 5,000 rows. Formulas are never evaluated. ${MINIMUM[kind]}${isMoney(kind) ? " Files over 500 rows are checked against the contract rules at commit, not in the preview." : ""}`}>
            <Input id="file" type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </Field>
          <div className="mb-4">
            <Button type="button" variant="outline" size="sm" disabled={template.busy}
              onClick={() => void template.run(`imports/template.csv?kind=${kind}`, `softwarden-${kind}-template.csv`)}>
              Download template (.csv)<span className="sr-only"> for {kindWord(kind)}</span>
            </Button>
            {template.error && <p role="alert" className="mt-1 text-sm text-error-foreground">{template.error}</p>}
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            <Button asChild variant="outline"><Link to="/imports">Cancel</Link></Button>
            <Button type="submit">Upload</Button>
          </div>
        </form>
      )}
    </>
  );
}

function RunPage({ id }: { id: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const fromUpload = (location.state as { run?: RunDetail } | null)?.run;
  const load = useLoad<RunDetail>(`imports/${id}`, { strict: true });
  const [run, setRun] = useState<RunDetail | null>(fromUpload?.id === id ? fromUpload : null);
  const [rev, setRev] = useState(0);
  const [notice, setNotice] = useState("");
  const [problem, setProblem] = useState("");
  const [committing, setCommitting] = useState(false);
  const [commitFailed, setCommitFailed] = useState(false);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  const loaded = load.data;
  useEffect(() => { if (loaded) setRun((r) => r ?? loaded); }, [loaded]);

  const stage = !run ? "loading" : committing ? "commit"
    : run.status === "committed" ? "results" : run.status === "refused" ? "commit" : run.status === "cancelled" ? "cancelled"
    : run.status === "uploaded" ? (run.needs_mapping && !run.purged ? "map" : "validate") : "preview";
  const prev = useRef(fromUpload ? "start" : "loading");
  useEffect(() => {
    if (stage === "loading") return;
    if (prev.current !== "loading" && prev.current !== stage) requestAnimationFrame(() => document.getElementById("step-heading")?.focus());
    prev.current = stage;
  }, [stage]);

  function apply(r: RunDetail) { setRun(r); setRev((v) => v + 1); }

  /** Changes are applied one at a time so two quick choices can't land out of order. */
  function patch(body: Record<string, unknown>, said: (r: RunDetail) => string) {
    const step = chain.current.then(async () => {
      try {
        // Each choice names its own change (one row's resolution) and sends back nothing read earlier.
        const r = await api<RunDetail>(`imports/${id}`, { method: "PATCH", body, headers: ANY_REVISION });
        apply(r);
        setProblem("");
        setNotice(said(r));
      } catch (e) {
        setProblem(e instanceof Problem ? e.detail : "Couldn't save that choice. Try again.");
      }
    });
    chain.current = step;
    return step;
  }

  async function commit() {
    if (!run) return;
    setCommitting(true);
    setCommitFailed(false);
    setProblem("");
    try {
      apply(await api<RunDetail>(`imports/${id}/commit`, { method: "POST" }));
    } catch (e) {
      if (e instanceof Problem && e.status === 409 && e.type === REFUSED) {
        setRefusal({ detail: e.detail, changed: (e.body.changed as Refusal["changed"]) ?? [], errorRows: (e.body.error_rows as number[]) ?? [] });
        try { apply(await api<RunDetail>(`imports/${id}`)); } catch { setRun({ ...run, status: "refused", refused_reason: e.detail }); }
      } else {
        setProblem(e instanceof Problem ? e.detail : "The commit didn't finish. Nothing was imported.");
        setCommitFailed(true);
      }
    } finally {
      setCommitting(false);
    }
  }

  async function startOver() {
    if (!run) return;
    setCancelling(true);
    const failed = await attempt(() => api(`imports/${id}/cancel`, { method: "POST" }));
    setCancelling(false);
    if (failed) setProblem(failed.detail);
    else navigate("/imports/new");
  }

  async function rerun() {
    setProblem("");
    try {
      const fresh = await api<RunDetail>(`imports/${id}/rerun`, { method: "POST" });
      navigate(`/imports/${fresh.id}`, { state: { run: fresh } });
    } catch (e) {
      setProblem(e instanceof Problem ? e.detail : "Couldn't rerun the preview. Try again.");
    }
  }

  async function submitMapping(mapping: Record<string, string>, save: boolean): Promise<string[] | null> {
    try {
      // The mapping is the whole choice for this upload, made in this step (any revision of the run).
      apply(await api<RunDetail>(`imports/${id}`, { method: "PATCH", body: { mapping, ...(save ? { save_mapping: true } : {}) }, headers: ANY_REVISION }));
      return null;
    } catch (e) {
      if (e instanceof Problem) return e.errors.mapping ?? e.errors.detail ?? [e.detail];
      return ["Couldn't save the mapping. Try again."];
    }
  }

  if (load.status === 404 && !run) return <NotFound what="import" back={{ to: "/imports", label: "Imports" }} trail={[{ to: "/imports", label: "Imports" }]} />;
  if (load.error && !run) {
    return (
      <>
        <Breadcrumb trail={[{ to: "/imports", label: "Imports" }]} current="Import" />
        <PageHead title="Import" />
        <ErrorBanner message={load.status === 404 ? "That import doesn't exist." : `Couldn't load this import. ${load.error}`} onRetry={load.status === 404 ? undefined : load.reload} />
      </>
    );
  }
  if (!run) return <><PageHead title="Import" /><Loading what="the import" rows={3} /></>;

  const mapping = run.needs_mapping || stage === "map";
  const steps = stepsFor(!!mapping);
  const at = (name: string) => steps.indexOf(name);
  const current = stage === "map" ? at("Map columns") : stage === "validate" ? at("Validate & match") : stage === "preview" ? at("Preview")
    : stage === "commit" ? at("Commit") : stage === "results" ? at("Results") : -1;
  const sourceNote = run.source ? ` · source ${run.source.name}` : "";

  return (
    <>
      <Breadcrumb trail={[{ to: "/imports", label: "Imports" }]} current={run.filename} />
      <PageHead title={`Import ${kindWord(run.kind)}`} desc={<span className="[overflow-wrap:anywhere]">{run.filename} · {when(run.created_at)}{sourceNote} · <StatusChip status={run.status} /></span>} docTitle={`Import ${run.filename}`} />
      <Steps steps={steps} current={current} />
      {problem && <Alert>{problem}</Alert>}

      {run.purged && stage !== "cancelled" ? <PurgedRun run={run} /> : (
        <>
          {stage === "map" && <MapStep run={run} onSubmit={submitMapping} onStartOver={() => void startOver()} cancelling={cancelling} />}
          {stage === "validate" && (
            <>
              <StepHeading>Validate & match</StepHeading>
              <p className="mb-3">This file hasn't been previewed yet.</p>
              <div className="flex gap-2"><Button variant="outline" onClick={load.reload}>Check again</Button><Button variant="outline" disabled={cancelling} onClick={() => void startOver()}>Start over</Button></div>
            </>
          )}
          {stage === "preview" && (
            <PreviewStep run={run} rev={rev} notice={notice} commitFailed={commitFailed} cancelling={cancelling}
              onPatch={patch} onCommit={() => void commit()} onStartOver={() => void startOver()} />
          )}
          {stage === "commit" && (committing ? <Committing run={run} /> : <RefusedStep run={run} refusal={refusal} onRerun={() => void rerun()} />)}
          {stage === "results" && <ResultsStep run={run} rev={rev} />}
          {stage === "cancelled" && (
            <>
              <StepHeading>Canceled</StepHeading>
              <EmptyState title="This import was canceled" action={<Button asChild><Link to="/imports/new">New import</Link></Button>}>Nothing was imported.</EmptyState>
            </>
          )}
        </>
      )}
    </>
  );
}

function PurgedRun({ run }: { run: RunDetail }) {
  const c = run.counts;
  return (
    <>
      <StepHeading>{STATUS[run.status].label}</StepHeading>
      <EmptyState title={`File and row details deleted on ${day(run.purged_at)}`} action={<Button asChild variant="outline"><Link to="/imports">Back to Imports</Link></Button>}>
        Runs older than 30 days keep only their counts: {plural(num(c, "total"), "row")}
        {run.status === "committed" ? `, ${num(c, "created")} created, ${num(c, "updated")} updated, ${num(c, "unchanged")} unchanged.` : `, ${num(c, "create")} to create, ${num(c, "update")} to update, ${num(c, "unchanged")} unchanged, ${num(c, "error")} with errors.`}
      </EmptyState>
    </>
  );
}

// ---- Map columns ----------------------------------------------------------------------------------------------------

function MapStep({ run, onSubmit, onStartOver, cancelling }: {
  run: RunDetail; onSubmit: (m: Record<string, string>, save: boolean) => Promise<string[] | null>; onStartOver: () => void; cancelling: boolean;
}) {
  const headers = run.headers ?? [];
  const fields = run.fields ?? [];
  const saved = run.mapping && Object.keys(run.mapping).length > 0 ? run.mapping : null;
  const [choice, setChoice] = useState<Record<string, string>>(() => Object.fromEntries(headers.map((h) => [h, saved?.[h] ?? run.suggested_mapping?.[h] ?? "ignore"])));
  const [save, setSave] = useState(!!saved);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const targets = [...fields, ...(fields.some((f) => f.value === "external_key") ? [] : [{ value: "external_key", label: "External key", required: false }])];
  const required = fields.filter((f) => f.required).map((f) => f.label);
  useEffect(() => { if (errors.length) box.current?.focus(); }, [errors]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    const used = new Set(Object.values(choice));
    const missing = fields.filter((f) => f.required && !used.has(f.value)).map((f) => f.label);
    if (missing.length) return setErrors([`Map a column to ${missing.join(" and ")} (required) to continue.`]);
    setBusy(true);
    setErrors((await onSubmit(choice, save && !!run.source)) ?? []);
    setBusy(false);
  }
  return (
    <>
      <StepHeading>Map columns</StepHeading>
      <p className="mb-3 max-w-prose text-muted-foreground">These headings aren't the template's. Choose what each column holds; columns set to Ignore are skipped.{required.length ? ` ${required.join(" and ")} ${required.length === 1 ? "is" : "are"} required.` : ""}</p>
      {errors.length > 0 && (
        <div ref={box} tabIndex={-1} role="alert" className="mb-4 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3 outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <strong>Fix the mapping:</strong>
          <ul className="ml-5 list-disc">{errors.map((m) => <li key={m}>{m}</li>)}</ul>
        </div>
      )}
      <form onSubmit={submit} noValidate>
        <TableRegion label="Column mapping table">
          <Table>
            <TableCaption className="sr-only">Each heading in {run.filename} and the field it fills</TableCaption>
            <TableHeader><TableRow><TableHead scope="col">Heading in your file</TableHead><TableHead scope="col">Fills</TableHead></TableRow></TableHeader>
            <TableBody>
              {headers.map((h, i) => (
                <TableRow key={h}>
                  <TableHead scope="row" className="font-normal text-foreground [overflow-wrap:anywhere]">{h}</TableHead>
                  <TableCell>
                    <Select aria-label={`Field for column ${h}`} id={`map-${i}`} className="max-w-xs" value={choice[h] ?? "ignore"} onChange={(e) => setChoice({ ...choice, [h]: e.target.value })}>
                      <option value="ignore">Ignore this column</option>
                      {targets.map((f) => <option key={f.value} value={f.value}>{f.label}{f.required ? " (required)" : ""}</option>)}
                    </Select>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
        {run.source && (
          <label className="mt-4 flex items-center gap-2">
            <input type="checkbox" className="h-4 w-4 accent-primary" checked={save} onChange={(e) => setSave(e.target.checked)} />
            Save this mapping for {run.source.name}
          </label>
        )}
        <div className="mt-4 flex flex-wrap gap-2">
          <Button type="submit" disabled={busy}>{busy ? "Checking…" : "Continue"}</Button>
          <Button type="button" variant="outline" disabled={cancelling} onClick={onStartOver}>Start over</Button>
        </div>
      </form>
    </>
  );
}

// ---- preview --------------------------------------------------------------------------------------------------------

function PreviewStep({ run, rev, notice, commitFailed, cancelling, onPatch, onCommit, onStartOver }: {
  run: RunDetail; rev: number; notice: string; commitFailed: boolean; cancelling: boolean;
  onPatch: (body: Record<string, unknown>, said: (r: RunDetail) => string) => Promise<unknown>; onCommit: () => void; onStartOver: () => void;
}) {
  const c = run.counts;
  const report = useFileDownload();
  const changes = num(c, "create") + num(c, "update");
  const errorsLeft = num(c, "blocking_errors");
  const pending = num(c, "pending_decisions");
  const nothing = changes + num(c, "new_keys") === 0 && !errorsLeft && !pending;
  const reasons = [
    errorsLeft > 0 && `${plural(errorsLeft, "row has", "rows have")} errors. Fix the file and re-upload, or exclude ${errorsLeft === 1 ? "it" : "them"} to continue.`,
    pending > 0 && `${plural(pending, "choice")} still needed above.`,
    !errorsLeft && !pending && !run.can_commit && "Nothing to commit.",
  ].filter(Boolean) as string[];
  const p = run.panels;
  const choose = (group: "people" | "teams" | "vendors", key: string, value: "create" | "gap") =>
    onPatch({ decisions: { [group]: { [key]: value } } }, () => "Choice saved. The preview is updated.");
  const matchOne = (row: number, value: "create" | "match") => onPatch({ decisions: { matches: { [String(row)]: value } } }, () => "Choice saved. The preview is updated.");
  const matchedCount = num(p.matched as Record<string, number>, "people") + num(p.matched as Record<string, number>, "teams") + num(p.matched as Record<string, number>, "vendors");

  const notices = p.notices ?? [];
  return (
    <>
      <StepHeading>Preview</StepHeading>
      {notices.length > 0 && (
        <div role="note" data-testid="import-notices" className="mb-4 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground">
          <p className="font-semibold"><span aria-hidden="true">! </span>{num(c, "rehearsed") === 0 && isMoney(run.kind) ? "Checked at commit, not in this preview" : "Before you commit"}</p>
          {notices.map((n) => <p key={n} className="mt-1">{n}</p>)}
        </div>
      )}
      {nothing ? (
        <EmptyState title="Nothing to commit" action={<Button asChild><Link to="/imports">Done</Link></Button>}>
          {num(c, "unchanged") === num(c, "total") ? `All ${num(c, "total")} rows are unchanged. Nothing to commit.` : "No row will create or change a record. Nothing to commit."}
        </EmptyState>
      ) : null}
      {(p.people.length > 0 || p.teams.length > 0 || p.vendors.length > 0 || matchedCount > 0) && (
        <Card id="people-h" title="Owners, teams and vendors" note={matchedCount > 0 ? `Matched: ${[num(p.matched as Record<string, number>, "people") && plural(num(p.matched as Record<string, number>, "people"), "person", "people") + " by email", num(p.matched as Record<string, number>, "teams") && plural(num(p.matched as Record<string, number>, "teams"), "team") + " by name", num(p.matched as Record<string, number>, "vendors") && plural(num(p.matched as Record<string, number>, "vendors"), "vendor") + " by name"].filter(Boolean).join(" · ")}` : undefined}>
          {p.people.length + p.teams.length + p.vendors.length === 0 ? <p className="text-muted-foreground">Everything the file names already exists.</p> : (
            <>
              <p className="mb-2 text-sm text-muted-foreground">No match ({p.people.length + p.teams.length + p.vendors.length}). Person names never auto-match; choose for each value.</p>
              <div className="grid gap-3">
                {p.people.map((e) => <EntryChoice key={`p-${e.key}`} e={e} noun="person" make={`Create person “${e.name || e.email}”`} gap="Leave ownership gap" onChoose={(v) => choose("people", e.key, v)} />)}
                {p.teams.map((e) => <EntryChoice key={`t-${e.key}`} e={e} noun="team" make={`Create team “${e.name}”`} gap="Leave support gap" onChoose={(v) => choose("teams", e.key, v)} />)}
                {p.vendors.map((e) => <EntryChoice key={`v-${e.key}`} e={e} noun="vendor" make={`Create vendor “${e.name}”`} gap="Leave vendor gap" onChoose={(v) => choose("vendors", e.key, v)} />)}
              </div>
            </>
          )}
        </Card>
      )}
      {p.matches.length > 0 && (
        <Card id="decide-h" title={`Needs your decision (${p.matches.length})`} note="Names never merge records. Matching needs your explicit approval.">
          <div className="grid gap-4">
            {p.matches.map((m) => <MatchChoice key={m.row_number} m={m} onChoose={(v) => void matchOne(m.row_number, v)} />)}
          </div>
        </Card>
      )}
      <RowsSection run={run} rev={rev} mode="preview" onPatch={onPatch} notice={notice} />
      <div className="mt-5 flex flex-wrap items-center justify-end gap-x-3 gap-y-2 border-t pt-4">
        {reasons.length > 0 && !nothing && <p id="commit-reason" className="mr-auto text-sm text-error-foreground"><span aria-hidden="true">⚠ </span>{reasons.join(" ")}</p>}
        {(errorsLeft > 0 || num(c, "excluded") > 0) && (
          <Button variant="outline" disabled={report.busy} onClick={() => void report.run(`imports/${run.id}/errors.csv`, "import-errors.csv")}>Download error report</Button>
        )}
        <Button variant="outline" disabled={cancelling} onClick={onStartOver}>Start over</Button>
        {!nothing && (
          <Button disabled={!run.can_commit} aria-describedby={reasons.length ? "commit-reason" : undefined} onClick={onCommit}>
            {commitFailed ? "Retry commit" : `Commit ${plural(changes, "change")}`}
          </Button>
        )}
      </div>
      {report.error && <p role="alert" className="mt-2 text-right text-sm text-error-foreground">{report.error}</p>}
    </>
  );
}

/** A choice shows at once and is confirmed when the server answers; the next preview from the server wins. */
function useChoice<T extends string>(saved: T | null) {
  const [choice, setChoice] = useState<T | null>(saved);
  useEffect(() => { setChoice(saved); }, [saved]);
  return [choice, setChoice] as const;
}

function EntryChoice({ e, noun, make, gap, onChoose }: { e: Entry; noun: string; make: string; gap: string; onChoose: (v: "create" | "gap") => void }) {
  const [choice, setChoice] = useChoice<"create" | "gap">(e.decision);
  const pick = (v: "create" | "gap") => { setChoice(v); onChoose(v); };
  const used = e.fields.map((f) => FIELD_WORD[f] ?? f.replace(/_/g, " ")).join(", ");
  const group = `${noun}-${e.key}`;
  return (
    <fieldset className="grid gap-1.5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start sm:gap-x-6">
      <legend className="sr-only">{noun === "team" || noun === "vendor" ? `${label(noun)} ${e.name}` : e.email || e.name}, in {used || "the file"}</legend>
      <div aria-hidden="true" className="[overflow-wrap:anywhere]">
        <span className="font-medium">{noun === "person" ? e.email || e.name : `${label(noun)} “${e.name}”`}</span>
        <span className="block text-sm text-muted-foreground">{used} · {e.rows.length === 1 ? "row" : "rows"} {e.rows.slice(0, 8).join(", ")}{e.rows.length > 8 && ` and ${e.rows.length - 8} more`}</span>
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1">
        <label className="flex items-center gap-2"><input type="radio" name={group} className="h-4 w-4 accent-primary" checked={choice === "create"} onChange={() => pick("create")} />{make}</label>
        <label className="flex items-center gap-2"><input type="radio" name={group} className="h-4 w-4 accent-primary" checked={choice === "gap"} onChange={() => pick("gap")} />{gap}</label>
      </div>
    </fieldset>
  );
}

function MatchChoice({ m, onChoose }: { m: MatchEntry; onChoose: (v: "create" | "match") => void }) {
  const [choice, setChoice] = useChoice<"create" | "match">(m.decision);
  const pick = (v: "create" | "match") => { setChoice(v); onChoose(v); };
  return (
    <fieldset className="grid gap-1.5">
      <legend className="mb-1">
        Row {m.row_number} “{m.name}” has no ID or key. A record named “{m.candidate.name}” exists.
        {m.candidate.known_to_other_source && <span className="text-muted-foreground"> It is already known to another import source.</span>}
      </legend>
      <label className="flex items-center gap-2"><input type="radio" name={`match-${m.row_number}`} className="h-4 w-4 accent-primary" checked={choice === "create"} onChange={() => pick("create")} />Create a new record</label>
      <label className="flex items-center gap-2"><input type="radio" name={`match-${m.row_number}`} className="h-4 w-4 accent-primary" checked={choice === "match"} onChange={() => pick("match")} />Match to {m.candidate.name} (explicit approval)</label>
    </fieldset>
  );
}

function ExcludeBox({ r, onExclude }: { r: Row; onExclude: (r: Row, on: boolean) => void }) {
  const [on, setOn] = useChoice<"yes" | "no">(r.excluded ? "yes" : "no");
  return (
    <label className="flex items-center gap-2">
      <input type="checkbox" className="h-4 w-4 accent-primary" checked={on === "yes"} onChange={(e) => { setOn(e.target.checked ? "yes" : "no"); onExclude(r, e.target.checked); }} />
      <span>Exclude<span className="sr-only"> row {r.row_number}, {r.name}</span></span>
    </label>
  );
}

// ---- rows (preview and results) -------------------------------------------------------------------------------------

function RowsSection({ run, rev, mode, onPatch, notice }: {
  run: RunDetail; rev: number; mode: "preview" | "results";
  onPatch?: (body: Record<string, unknown>, said: (r: RunDetail) => string) => Promise<unknown>; notice?: string;
}) {
  const [params, setParams] = useSearchParams();
  const raw = params.get("outcome") ?? "";
  const outcome = OUTCOMES.some((o) => o.id === raw) ? (raw as Outcome) : "";
  const [text, setText] = useState(params.get("q") ?? "");
  const q = (params.get("q") ?? "").trim();
  const [offset, setOffset] = useState(0);
  useEffect(() => { setOffset(0); }, [outcome, q]);
  function update(changes: Record<string, string>) {
    const next = new URLSearchParams(params);
    // Typing the search replaces the history entry; any other change pushes one, so Back undoes it.
    for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
    setParams(next, { replace: Object.keys(changes).every((k) => k === "q") });
  }
  useEffect(() => {
    if (text.trim() === q) return;
    const t = setTimeout(() => update({ q: text.trim() }), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const limit = 100;
  const query = new URLSearchParams({ ...(outcome ? { outcome } : {}), ...(q ? { q } : {}), offset: String(offset), limit: String(limit), rev: String(rev) });
  const rows = useLoad<RowPage>(`imports/${run.id}/rows?${query}`);
  const c = run.counts;
  const refocus = useRef(false);
  const data = rows.data;
  useEffect(() => {
    if (refocus.current && data) {
      refocus.current = false;
      if (!document.activeElement || document.activeElement === document.body) document.getElementById("rows-status")?.focus();
    }
  }, [data]);

  function exclude(r: Row, on: boolean) {
    refocus.current = true;
    void onPatch?.(on ? { exclude: [r.row_number] } : { include: [r.row_number] }, (x) =>
      `Row ${r.row_number}, ${r.name}, ${on ? "excluded" : "included again"}. ${num(x.counts, "blocking_errors") ? `${plural(num(x.counts, "blocking_errors"), "error row")} left.` : "No error rows left."}`);
  }

  const tiles = OUTCOMES.filter((o) => mode === "preview" || o.id !== "error" || num(c, "error") > 0);
  return (
    <section aria-labelledby="rows-h" className="mb-2">
      <h3 id="rows-h" className="sr-only">Rows</h3>
      <div className="mb-3 flex flex-wrap gap-2" role="group" aria-label="Filter rows by outcome">
        {tiles.map((o) => (
          <button key={o.id} type="button" aria-pressed={outcome === o.id} onClick={() => update({ outcome: outcome === o.id ? "" : o.id })}
            className="flex min-w-24 flex-col items-start rounded-md border border-border-strong bg-card px-3 py-1.5 text-left text-[13px] text-muted-foreground aria-pressed:border-accent-line aria-pressed:bg-accent aria-pressed:text-accent-foreground">
            <span><span aria-hidden="true">{o.glyph} </span>{o.label}</span>
            <span className="tabular text-xl font-semibold text-foreground">{num(c, o.id)}</span>
          </button>
        ))}
      </div>
      {mode === "preview" && num(c, "needs_confirmation") > 0 && (
        <p className="mb-3 text-sm text-muted-foreground">{plural(num(c, "needs_confirmation"), "update")} will mark confirmed records as Needs confirmation.</p>
      )}
      <form role="search" className="mb-3 flex max-w-sm flex-col gap-1.5" onSubmit={(e) => { e.preventDefault(); update({ q: text.trim() }); }}>
        <Label htmlFor="rows-search">Search row names</Label>
        <Input id="rows-search" type="search" value={text} onChange={(e) => setText(e.target.value)} />
      </form>
      <p id="rows-status" role="status" tabIndex={-1} className="mb-2 min-h-5 text-sm text-muted-foreground outline-none">{notice}</p>
      {rows.error && <ErrorBanner message={`Couldn't load the rows. ${rows.error}`} onRetry={rows.reload} />}
      {!data && !rows.error && <Loading what="rows" rows={4} />}
      {data && data.results.length === 0 && (
        <EmptyState title="No rows">{q ? `No row name matches "${q}".` : outcome ? `No ${outcome} rows.` : "This file has no rows."}</EmptyState>
      )}
      {data && data.results.length > 0 && (
        <>
          <TableRegion label="Import rows table">
            <Table>
              <TableCaption className="sr-only">Rows of {run.filename}, in file order</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col" aria-sort="ascending">Row</TableHead><TableHead scope="col">Name</TableHead><TableHead scope="col">Outcome</TableHead>
                <TableHead scope="col">Details</TableHead><TableHead scope="col">{mode === "preview" ? "Exclude" : "Record"}</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {data.results.map((r) => <RowLine key={r.row_number} r={r} kind={run.kind} mode={mode} onExclude={exclude} />)}
              </TableBody>
            </Table>
          </TableRegion>
          <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
            <span>Showing rows {offset + 1}–{offset + data.results.length} of {data.count}.</span>
            {offset > 0 && <Button size="sm" variant="outline" onClick={() => setOffset(Math.max(0, offset - limit))}>Previous page</Button>}
            {offset + limit < data.count && <Button size="sm" variant="outline" onClick={() => setOffset(offset + limit)}>Next page</Button>}
          </div>
        </>
      )}
    </section>
  );
}

function RowLine({ r, kind, mode, onExclude }: { r: Row; kind: Kind; mode: "preview" | "results"; onExclude: (r: Row, on: boolean) => void }) {
  const o = OUTCOMES.find((x) => x.id === r.outcome) ?? OUTCOMES[2];
  // Records open their own pages; contracts and licence pools theirs (`/contracts/<id>`, `/license-pools/<id>`).
  const link = (x: { id: string; name: string }, k: string = kind) => {
    const to = moneyPath(k, x.id);
    return to ? <Link to={to} className="inline-block min-h-6 leading-6">{x.name}</Link> : <RecordLink record={{ ...x, kind: k }} />;
  };
  const changes = Object.entries(r.changes ?? {});
  const computed = r.computed && Object.keys(r.computed).length > 0 ? r.computed : null;
  const empty = !changes.length && !r.errors.length && !r.warnings.length && !r.gaps.length && !r.needs_decision.length && !r.needs_confirmation && !r.possible_match && !computed;
  return (
    <TableRow className="align-top">
      <TableCell className="tabular">{r.row_number}</TableCell>
      <TableHead scope="row" className="min-w-36 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">{r.name || <span className="font-normal text-muted-foreground">(no name)</span>}</TableHead>
      <TableCell>
        <span className={chip({ state: r.outcome === "error" ? "overdue" : "neutral" })}><span aria-hidden="true">{o.glyph}</span>{o.label}</span>
      </TableCell>
      <TableCell className="whitespace-normal">
        {empty && <span className="text-muted-foreground">—</span>}
        {r.errors.length > 0 && (
          <ul>{r.errors.map((m, i) => <li key={i} className="text-error-foreground"><span aria-hidden="true">✕ </span><span className="sr-only">Error: </span>{columnPrefix(m.column)}{m.message}</li>)}</ul>
        )}
        {changes.length > 0 && (
          <ul>{changes.map(([f, [from, to]]) => <li key={f}>{label(f)}: <span className="text-muted-foreground">{show(from)}</span> → <strong className="font-semibold">{show(to)}</strong></li>)}</ul>
        )}
        {r.needs_confirmation && <p className="mt-1"><span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Will need confirmation</span></p>}
        {r.needs_decision.map((m, i) => <p key={i} className="mt-1 text-sm"><span aria-hidden="true">◐ </span>Needs your decision: {m.message}</p>)}
        {r.possible_match && !r.needs_decision.length && <p className="mt-1 text-sm">Possible match: {isMoney(kind) ? link(r.possible_match) : r.possible_match.name}</p>}
        {r.warnings.map((m, i) => <p key={i} className="mt-1 text-sm text-muted-foreground"><span className="sr-only">Note: </span>{columnPrefix(m.column)}{m.message}</p>)}
        {r.gaps.length > 0 && <p className="mt-1"><GapChips gaps={r.gaps} /></p>}
        {r.matched && r.outcome !== "create" && mode === "preview" && <p className="mt-1 text-sm text-muted-foreground">Matches {isMoney(kind) ? link(r.matched) : r.matched.name}</p>}
        {computed && <ComputedText kind={kind} c={computed} />}
      </TableCell>
      <TableCell className="whitespace-normal">
        {mode === "preview"
          ? (r.outcome === "error" || r.excluded) && (
            <ExcludeBox r={r} onExclude={onExclude} />
          )
          : r.result ? link(r.result, r.result.kind) : r.matched ? link(r.matched) : <span className="text-muted-foreground">—</span>}
      </TableCell>
    </TableRow>
  );
}

/** What the preview worked out for a contract or licence pool row, in words. Money stays the API's decimal text,
 *  regrouped ("12,000.00 USD"); overuse is said in words, never by colour alone. */
function ComputedText({ kind, c }: { kind: Kind; c: Computed }) {
  if (kind === "license_pool") {
    const over = c.utilization != null && c.utilization > 100;
    return (
      <p className="mt-1 text-sm" data-testid="computed">
        <span className="font-medium">Utilization: </span>
        <span className="tabular">{c.seats_assigned ?? 0} of {c.seats_purchased ?? 0} {c.seats_purchased === 1 ? "seat" : "seats"}{c.utilization != null ? ` (${c.utilization}%)` : " (none purchased)"}</span>
        {over && <> <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Overused by {(c.seats_assigned ?? 0) - (c.seats_purchased ?? 0)}</span></>}
      </p>
    );
  }
  const money = (amount: string | null | undefined) => (amount ? `${groupDigits(amount, currencyDecimals(c.currency ?? ""))} ${c.currency ?? ""}`.trim() : null);
  const label = c.deadline_label ? DEADLINE_LABEL[c.deadline_label] ?? c.deadline_label : null;
  const allocation = c.allocation ?? [];
  return (
    <div className="mt-1 text-sm" data-testid="computed">
      <p><span className="font-medium">Annualized cost: </span>{money(c.annualized_cost) ?? <span className="text-muted-foreground">none (no amount)</span>}
        {c.annualization_note && <span className="text-muted-foreground"> · {c.annualization_note}</span>}</p>
      <p><span className="font-medium">Cancellation deadline: </span>
        {c.cancellation_deadline ? <span className="tabular">{c.cancellation_deadline}</span> : <GapChip label="No deadline" />}
        {label && <> <span className={chip({ state: c.deadline_label === "assumed" ? "gap" : "neutral" })}>{c.deadline_label === "assumed" && <span aria-hidden="true">⚠</span>}{label}</span></>}
        {c.deadline_explanation && <span className="block text-muted-foreground">{c.deadline_explanation}</span>}</p>
      {allocation.length > 0 && (
        <>
          <p className="font-medium">Cost by line:</p>
          <ul className="ml-5 list-disc">
            {allocation.map((a, i) => (
              <li key={a.record?.id ?? `rest-${i}`}>
                {a.record ? a.record.name : <span className="italic">Not allocated</span>}: <span className="tabular">{percentText(a.share)}%</span>
                {money(a.amount) && <span className="tabular text-muted-foreground"> ({money(a.amount)} a year)</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

// ---- commit, refused, results ---------------------------------------------------------------------------------------

function Committing({ run }: { run: RunDetail }) {
  const n = num(run.counts, "create") + num(run.counts, "update");
  return (
    <>
      <StepHeading>Commit</StepHeading>
      <div className="max-w-2xl rounded-lg border bg-card p-4">
        <p role="status" className="mb-2">Committing {plural(n, "change")}…</p>
        <div role="progressbar" aria-label="Committing" aria-valuetext={`Committing ${plural(n, "change")}`} className="h-2 overflow-hidden rounded-full bg-muted">
          <div className="h-full w-1/3 rounded-full bg-primary motion-safe:animate-pulse" />
        </div>
      </div>
    </>
  );
}

function RefusedStep({ run, refusal, onRerun }: { run: RunDetail; refusal: Refusal | null; onRerun: () => void }) {
  const names = refusal?.changed.map((x) => x.name).filter(Boolean) ?? [];
  return (
    <>
      <StepHeading>Commit</StepHeading>
      <Alert>
        <p className="font-semibold"><span aria-hidden="true">! </span>Nothing was imported.</p>
        <p className="mt-1">{refusal?.detail || run.refused_reason || "The commit was refused."}</p>
        {names.length > 0 && <ul className="mt-1 ml-5 list-disc">{names.map((n, i) => <li key={i}>{n}</li>)}</ul>}
      </Alert>
      <div className="flex flex-wrap gap-2">
        {!run.purged && <Button onClick={onRerun}>Rerun preview</Button>}
        <Button asChild variant="outline"><Link to="/imports">Back to Imports</Link></Button>
      </div>
    </>
  );
}

function ResultsStep({ run, rev }: { run: RunDetail; rev: number }) {
  const c = run.counts;
  const report = useFileDownload();
  const extras = [["People created", "people_created"], ["Teams created", "teams_created"], ["Vendors created", "vendors_created"], ["External keys stored", "keys_stored"]] as const;
  const bad = num(c, "excluded") + num(c, "error") > 0;
  return (
    <>
      <StepHeading>Results</StepHeading>
      <div className="mb-5 rounded-lg border bg-card p-4">
        <p className="font-semibold text-success-foreground"><span aria-hidden="true">✓ </span>Import committed {when(run.committed_at)} by {run.uploaded_by?.name ?? "someone"}{run.source ? ` · source ${run.source.name}` : ""}</p>
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
          <li>Created <strong className="tabular">{num(c, "created")}</strong></li>
          <li>Updated <strong className="tabular">{num(c, "updated")}</strong></li>
          <li>Unchanged <strong className="tabular">{num(c, "unchanged")}</strong></li>
          <li>Excluded <strong className="tabular">{num(c, "excluded")}</strong></li>
          {extras.filter(([, k]) => num(c, k) > 0).map(([l, k]) => <li key={k}>{l} <strong className="tabular">{num(c, k)}</strong></li>)}
        </ul>
        <p className="mt-2 text-muted-foreground">{isMoney(run.kind)
          ? `Every change is in the ${run.kind === "contract" ? "contracts'" : "license pools'"} History, naming this import. An import can't be undone; edit them or import a corrected file.`
          : "New records are Unreviewed. Ones without owners or other details show as gaps in Needs attention. An import can't be undone; edit the records or import a corrected file."}</p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {run.kind === "contract" ? <Button asChild><Link to="/contracts">Open Contracts</Link></Button>
            : run.kind === "license_pool" ? <Button asChild><Link to="/license-pools">Open License pools</Link></Button>
              : <Button asChild><Link to="/">Review gaps in Needs attention</Link></Button>}
          <Button asChild variant="outline"><Link to="/imports/new">New import</Link></Button>
          {bad && (
            <>
              <Button variant="outline" disabled={report.busy} onClick={() => void report.run(`imports/${run.id}/errors.csv`, "import-errors.csv")}>Download error report</Button>
              <span className="text-sm text-muted-foreground">Kept until {run.purge_after}</span>
            </>
          )}
        </div>
        {report.error && <p role="alert" className="mt-2 text-sm text-error-foreground">{report.error}</p>}
      </div>
      <RowsSection run={run} rev={rev} mode="results" />
    </>
  );
}

// ---- import sources (Admin) -----------------------------------------------------------------------------------------

export function ImportSources() {
  const me = useMe();
  if (me.role !== "admin") return <NoAccess title="Import sources" who="Admins" />;
  return <SourcesPage />;
}

function Modal({ title, description, open, onClose, children }: {
  title: string; description?: string; open: boolean; onClose: () => void; children: React.ReactNode;
}) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus} onCloseAutoFocus={(e) => {
        returnFocus.onCloseAutoFocus(e);
        // The opener may be gone (a deleted row); fall back to the page heading.
        if (!document.activeElement || document.activeElement === document.body) document.querySelector<HTMLElement>("h1")?.focus();
      }}>
        <DialogHeader>
          <DialogTitle className="[overflow-wrap:anywhere]">{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        {open && children}
      </DialogContent>
    </Dialog>
  );
}

function SourcesPage() {
  const sources = useLoad<Source[]>("import-sources");
  const [dialog, setDialog] = useState<{ mode: "add" } | { mode: "rename" | "delete"; source: Source } | null>(null);
  const [notice, setNotice] = useState("");
  const add = <Button onClick={() => setDialog({ mode: "add" })}>Add source</Button>;
  const done = (text: string) => { setDialog(null); setNotice(text); sources.reload(); };
  return (
    <>
      <PageHead title="Import sources" desc="Name the systems your CSVs come from. External keys from a source match records on re-import, and a source remembers its column mapping.">
        {sources.data && sources.data.length > 0 && add}
      </PageHead>
      <SuccessNote text={notice} />
      {sources.error && <ErrorBanner message={`Couldn't load import sources. ${sources.error}`} onRetry={sources.reload} />}
      {!sources.data && !sources.error && <Loading what="import sources" rows={3} />}
      {sources.data && sources.data.length === 0 && (
        <EmptyState title="No import sources yet" action={add}>A source is optional. Add one when a spreadsheet or system keeps its own IDs for the records, contracts or license pools it lists.</EmptyState>
      )}
      {sources.data && sources.data.length > 0 && (
        <TableRegion label="Import sources table">
          <Table>
            <TableCaption className="sr-only">Import sources, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col" aria-sort="ascending">Name</TableHead><TableHead scope="col">Kind</TableHead><TableHead scope="col">External keys</TableHead>
              <TableHead scope="col">Saved mapping</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {sources.data.map((s) => {
                const mapped = Object.keys(s.column_mapping ?? {}).length;
                return (
                  <TableRow key={s.id}>
                    <TableHead scope="row" className="whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">{s.name}</TableHead>
                    <TableCell>{IMPORT_KIND_PLURAL[s.kind] ?? s.kind}</TableCell>
                    <TableCell className="tabular">{s.identifiers}</TableCell>
                    <TableCell>{mapped ? `${plural(mapped, "column")} mapped` : <span className="text-muted-foreground">None</span>}</TableCell>
                    <TableCell className="text-right">
                      <span className="inline-flex gap-2">
                        <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "rename", source: s })}>Rename<span className="sr-only"> {s.name}</span></Button>
                        <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "delete", source: s })}>Delete<span className="sr-only"> {s.name}</span></Button>
                      </span>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <Modal open={dialog?.mode === "add"} title="Add import source" onClose={() => setDialog(null)}
        description="Name the system or spreadsheet. The kind can't change later.">
        <SourceForm onCancel={() => setDialog(null)} onDone={(s) => done(`Added ${s.name}.`)} />
      </Modal>
      <Modal open={dialog?.mode === "rename"} title={`Rename ${dialog && "source" in dialog ? dialog.source.name : ""}`} onClose={() => setDialog(null)}>
        {dialog?.mode === "rename" && (
          <OpenedEdit<Source> path={`import-sources/${dialog.source.id}`} what="import source">
            {(source, write) => <SourceForm source={source} write={write} onCancel={() => setDialog(null)} onDone={(s) => done(`Renamed to ${s.name}.`)} />}
          </OpenedEdit>
        )}
      </Modal>
      <Modal open={dialog?.mode === "delete"} title={`Delete ${dialog && "source" in dialog ? dialog.source.name : ""}?`} onClose={() => setDialog(null)}
        description="Imports that used it stay in the history. A source with stored external keys can't be deleted.">
        {dialog?.mode === "delete" && <DeleteForm source={dialog.source} onCancel={() => setDialog(null)} onDone={() => done(`Deleted ${dialog.source.name}.`)} />}
      </Modal>
    </>
  );
}

function SourceForm({ source, write, onCancel, onDone }: { source?: Source; write?: OpenedWrite; onCancel: () => void; onDone: (s: Source) => void }) {
  const me = useMe();
  const [name, setName] = useState(source?.name ?? "");
  const [kind, setKind] = useState<Kind>("application");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!name.trim()) return setErrors({ name: ["Enter a name."] });
    setBusy(true);
    const out: { s?: Source } = {};
    const failed = await attempt(async () => {
      out.s = await api<Source>(source ? `import-sources/${source.id}` : "import-sources", { method: source ? "PATCH" : "POST", body: source ? { name: name.trim() } : { name: name.trim(), kind }, headers: await write?.ifMatch() });
    });
    setBusy(false);
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    if (out.s) onDone(out.s);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="name" label="Name (required)" errors={errors}><Input id="name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} /></Field>
      {!source && (
        <Field id="kind" label="Kind" errors={errors}>
          <Select id="kind" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            {kindsFor(me.capabilities).map((k) => <option key={k} value={k}>{IMPORT_KIND_PLURAL[k]}</option>)}
          </Select>
        </Field>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : source ? "Save name" : "Add source"}</Button>
      </DialogFooter>
    </form>
  );
}

function DeleteForm({ source, onCancel, onDone }: { source: Source; onCancel: () => void; onDone: () => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(`import-sources/${source.id}`);
  async function remove() {
    setBusy(true);
    setError("");
    const failed = await attempt(async () => api(`import-sources/${source.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) }));
    setBusy(false);
    if (failed?.status === 412) setError(staleDeleteText("import source"));
    else if (failed) setError(failed.detail);
    else onDone();
  }
  return (
    <>
      {error && <Alert>{error}</Alert>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : "Delete source"}</Button>
      </DialogFooter>
    </>
  );
}
