/** Shadow IT (`/discovery/shadow-it`, capability `discovery.entra`, every role reads): third-party apps
 *  people sign in to or consented to in Microsoft Entra ID, from the `entra_shadow_it` source (GET discovery/shadow-it,
 *  keyset pages). Each row has the usage over 30 days (users estimated from a one-way sketch, sign-ins), the OAuth consent
 *  (admin, user with how many people, or none), the scopes and the high-risk ones, first seen and last sign-in, and
 *  whether the catalog has it (○ New, ✓ Linked to its record, ⊘ Dismissed). Editors decide a new app through its
 *  discovery candidate with the discovery queue's own dialog (create a record, link to an existing one, dismiss).
 *  Counts only: the API never returns who signed in or consented, and the page says why. Filters, sort and the page
 *  cursor live in the URL; Export CSV (hidden while platform staff impersonate, as the API refuses it). Loaded on first
 *  use. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, Problem, type Connector, type Observation } from "@/api";
import { EmptyState, ErrorBanner, Loading, PageHead, TableRegion } from "@/components/app";
import { RecordLink } from "@/components/record-link";
import { chip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { CommitInput, ExportCsvButton, filterProblem, SelectFilter, SortButton, useUrlFilters } from "@/components/url-filters";
import { DecisionDialog } from "@/features/discovery/discovery";
import { useTypedQuery, type QueryOf, type Schemas } from "@/lib/openapi";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";

type Row = Schemas["ShadowItApp"];
type Query = QueryOf<"discovery/shadow-it", "get">;
type Mode = "link" | "create" | "dismiss";

const FILTERS = ["status", "consent", "risky", "min_users", "q", "connector"] as const;
/** The URL's `status`: absent is the apps to decide (new); `all` sends no status filter. */
const STATUS_CHOICES = [
  { value: "", label: "To decide (not in the catalog)" }, { value: "linked", label: "Linked to a record" },
  { value: "dismissed", label: "Dismissed" }, { value: "all", label: "Every app" },
];
const CONSENT_CHOICES = [{ value: "admin", label: "Admin consent" }, { value: "user", label: "User consent" }, { value: "none", label: "No consent grant" }];
const RISKY_CHOICES = [{ value: "true", label: "Granted a high-risk scope" }, { value: "false", label: "No high-risk scope" }];
/** Sortable columns: the API's ordering field and what the first press does (counts most first, the name A to Z). */
const SORTS = {
  display_name: { label: "App", first: "" },
  users_30d: { label: "Users, 30 days", first: "-" },
  sign_ins_30d: { label: "Sign-ins, 30 days", first: "-" },
  consenting_users: { label: "Consent", first: "-" },
  first_seen_at: { label: "First seen", first: "-" },
} as const;
type SortKey = keyof typeof SORTS;
const DEFAULT_ORDER = "-users_30d";

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
const day = (iso: string | null) => (iso ? iso.slice(0, 10) : null);

const STATUS: Record<Row["status"], { glyph: string; text: string; state: "unreviewed" | "confirmed" | "neutral" }> = {
  new: { glyph: "○", text: "Not in the catalog", state: "unreviewed" },
  linked: { glyph: "✓", text: "Linked", state: "confirmed" },
  dismissed: { glyph: "⊘", text: "Dismissed", state: "neutral" },
};

function StatusChip({ status }: { status: Row["status"] }) {
  const s = STATUS[status];
  return <span className={chip({ state: s.state })} data-status={status}><span aria-hidden="true">{s.glyph}</span>{s.text}</span>;
}

function ConsentText({ r }: { r: Row }) {
  if (r.consent === "admin") return <><span className="font-semibold">Admin consent</span><span className="block text-sm text-muted-foreground">For everyone in the tenant</span></>;
  if (r.consent === "user") return <><span className="font-semibold">User consent</span><span className="block text-sm text-muted-foreground">{plural(r.consenting_users, "person", "people")} consented</span></>;
  return <span className="text-muted-foreground">No consent grant</span>;
}

function Scopes({ r }: { r: Row }) {
  if (r.scopes.length === 0) return <span className="text-muted-foreground">None granted</span>;
  const risky = new Set(r.risky_scopes.map((s) => s.toLowerCase()));
  const others = r.scopes.filter((s) => !risky.has(s.toLowerCase()));
  return (
    <div className="grid gap-1">
      {r.risky && (
        <span className={chip({ state: "pending" })} data-risky=""><span aria-hidden="true">!</span>{plural(r.risky_scopes.length, "high-risk scope")}</span>
      )}
      {r.risky_scopes.length > 0 && <span className="text-sm [overflow-wrap:anywhere]"><span className="sr-only">High-risk: </span><code>{r.risky_scopes.join(" ")}</code></span>}
      {others.length > 0 && <span className="text-sm text-muted-foreground [overflow-wrap:anywhere]">{r.risky ? "Also: " : ""}<code>{others.join(" ")}</code></span>}
    </div>
  );
}

export function ShadowItPage() {
  const me = useMe();
  if (!me.capabilities.includes("discovery.entra")) {
    return (
      <>
        <PageHead title="Shadow IT" />
        <p className="rounded-md border border-dashed border-border-strong bg-muted px-3 py-2" data-unavailable="">
          <span aria-hidden="true">⊘ </span><span className="font-semibold">Shadow IT isn't available.</span> It needs the Microsoft Entra discovery source, which this organization&apos;s edition doesn&apos;t include.
        </p>
      </>
    );
  }
  return <ShadowIt />;
}

function ShadowIt() {
  const me = useMe();
  const write = canWrite(me);
  const f = useUrlFilters(FILTERS);
  const invalidate = useInvalidate();
  const ordering = f.value("ordering") || DEFAULT_ORDER;
  const cursor = f.value("cursor");
  const query: Record<string, string> = {};
  for (const k of FILTERS) if (f.value(k) && k !== "status") query[k] = f.value(k);
  const status = f.value("status");
  if (status !== "all") query.status = status || "new";
  const exportSearch = new URLSearchParams(query).toString();
  if (ordering !== DEFAULT_ORDER) query.ordering = ordering;
  const q = useTypedQuery("discovery/shadow-it", { query: { ...query, ...(cursor ? { cursor } : {}) } as Partial<Query> });
  const connectors = useApiQuery<Connector[]>("connectors");
  const shadowConnectors = (connectors.data ?? []).filter((c) => c.source === "entra_shadow_it");
  const [notice, setNotice] = useState("");
  const [deciding, setDeciding] = useState<{ mode: Mode; obs: Observation } | null>(null);
  const [opening, setOpening] = useState("");
  const [openError, setOpenError] = useState("");
  const rows = q.data?.results;

  async function decide(r: Row, mode: Mode) {
    if (!r.candidate || opening) return;
    setOpening(r.id);
    setOpenError("");
    try {
      setDeciding({ mode, obs: await api<Observation>(`discovery/observations/${r.candidate.id}`) });
    } catch (e) {
      setOpenError(`Couldn't open ${r.name}'s candidate. ${e instanceof Problem ? e.detail : "Try again."}`);
    } finally {
      setOpening("");
    }
  }
  const sortBy = (key: SortKey) => {
    const active = ordering.replace(/^-/, "") === key;
    const next = active ? (ordering.startsWith("-") ? key : `-${key}`) : `${SORTS[key].first}${key}`;
    f.set("ordering", next === DEFAULT_ORDER ? "" : next);
  };
  const header = (key: SortKey) => {
    const active = ordering.replace(/^-/, "") === key;
    const descending = ordering.startsWith("-");
    const nextDescending = active ? !descending : SORTS[key].first === "-";
    return (
      <TableHead scope="col" aria-sort={active ? (descending ? "descending" : "ascending") : undefined}>
        <SortButton label={SORTS[key].label} active={active} descending={descending} onClick={() => sortBy(key)}
          next={key === "display_name" ? (nextDescending ? "Z to A" : "A to Z") : (nextDescending ? "most first" : "fewest first")} />
      </TableHead>
    );
  };
  const orderText = (() => {
    const key = ordering.replace(/^-/, "") as SortKey;
    const label = SORTS[key]?.label ?? "Users, 30 days";
    if (key === "display_name") return `by name, ${ordering.startsWith("-") ? "Z to A" : "A to Z"}`;
    return `by ${label.toLowerCase()}, ${ordering.startsWith("-") ? "most first" : "fewest first"}`;
  })();
  const noConnector = connectors.data && shadowConnectors.length === 0;
  const neverRun = shadowConnectors.length > 0 && shadowConnectors.every((c) => !c.last_run);
  const anything = f.filtered;
  /** Filters that narrow the list (a status other than "every app", or any other filter). */
  const narrowed = FILTERS.some((k) => k !== "status" && f.value(k)) || (!!status && status !== "all");

  return (
    <>
      <PageHead title="Shadow IT" desc="Third-party apps people in your Microsoft Entra tenant sign in to or have consented to, and whether the catalog has them.">
        <ExportCsvButton path={`discovery/shadow-it/export.csv${exportSearch ? `?${exportSearch}` : ""}`} fallback="softwarden-shadow-it.csv" what="the shadow IT overview" onDone={setNotice} />
      </PageHead>
      <p className="mb-4 max-w-prose rounded-md border border-info-border bg-info-bg px-3 py-2 text-sm" data-testid="privacy-note">
        <span aria-hidden="true">ⓘ </span><span className="font-semibold">Counts only.</span> Softwarden never stores who signed in or who consented: no user IDs, names or addresses.
        Users are estimated from a one-way sketch of the last 30 days of successful sign-ins (exact for small numbers), so the page can say how many people use an app, never which ones.
      </p>
      <SuccessNote text={notice} />
      {!write && <p className="mb-3 text-sm text-muted-foreground" data-testid="read-only">You can read the overview. Editors and Admins create, link and dismiss apps.</p>}
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter shadow IT" onSubmit={(e) => e.preventDefault()}>
        <SelectFilter id="shadow-status" label="Catalog status" value={status} onChange={(v) => f.set("status", v)} options={STATUS_CHOICES} all={null} />
        <SelectFilter id="shadow-consent" label="Consent" value={f.value("consent")} onChange={(v) => f.set("consent", v)} options={CONSENT_CHOICES} className="w-44" />
        <SelectFilter id="shadow-risky" label="High-risk scopes" value={f.value("risky")} onChange={(v) => f.set("risky", v)} options={RISKY_CHOICES} />
        <CommitInput id="shadow-min-users" label="At least this many users" value={f.value("min_users")} onChange={(v) => f.set("min_users", v, true)} numeric className="w-40" placeholder="Users" />
        <CommitInput id="shadow-q" label="Search" type="search" value={f.value("q")} onChange={(v) => f.set("q", v)} placeholder="Name, publisher or app ID" />
        {shadowConnectors.length > 1 && (
          <SelectFilter id="shadow-connector" label="Connector" value={f.value("connector")} onChange={(v) => f.set("connector", v)}
            options={shadowConnectors.map((c) => ({ value: c.id, label: c.name }))} all="Every connector" />
        )}
        {(anything || f.value("ordering")) && <Button type="button" variant="outline" onClick={f.clear}>Clear filters</Button>}
      </form>
      {openError && <ErrorBanner message={openError} />}
      <p className="sr-only" aria-live="polite">{rows ? `${plural(rows.length, "app")} on this page.` : ""}</p>
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={filterProblem(q.error, "Couldn't load the shadow IT overview.")} onRetry={() => void q.refetch()} />
          {anything && <Button variant="outline" onClick={f.clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the shadow IT overview" rows={5} />}
      {connectors.isError && rows && rows.length === 0 && (
        <ErrorBanner message={problemText(connectors.error, "Couldn't load the discovery connectors, so this page can't say whether shadow IT is set up.")} onRetry={() => void connectors.refetch()} />
      )}
      {rows && rows.length === 0 && (
        noConnector
          ? <EmptyState title="No shadow IT source is set up"
              action={me.role === "admin" && <Button asChild variant="outline"><Link to="/discovery/setup/entra_shadow_it">Set up Microsoft Entra shadow IT</Link></Button>}>
              {me.role === "admin"
                ? "Shadow IT reads your tenant's sign-in logs and OAuth consent grants. Set up the source with a Microsoft connection; it needs AuditLog.Read.All and Directory.Read.All."
                : "Shadow IT reads your tenant's sign-in logs and OAuth consent grants. An Admin sets up the source in Discovery."}
            </EmptyState>
          : neverRun
            ? <EmptyState title="Shadow IT hasn't run yet"
                action={me.role === "admin" && <Button asChild variant="outline"><Link to={`/discovery/connectors/${shadowConnectors[0].id}`}>Open {shadowConnectors[0].name}</Link></Button>}>
                {me.role === "admin" ? "The source is set up but hasn't finished a run. Run it from its connector page; apps appear here after the first run." : "The source is set up but hasn't finished a run. Apps appear here after the first run."}
              </EmptyState>
            : narrowed
              ? <EmptyState title="No apps match these filters" action={<Button variant="outline" onClick={f.clear}>Clear filters</Button>}>Try another catalog status or consent, or a lower number of users.</EmptyState>
              : status === "all"
                ? <EmptyState title="No third-party apps found">The last run found no third-party app people sign in to or consented to.</EmptyState>
                : <EmptyState title="No apps to decide" action={<Button asChild variant="outline"><Link to="/discovery/shadow-it?status=all">Show every app</Link></Button>}>
                    Every third-party app the last run found is in the catalog or dismissed. New ones appear after the next run.
                  </EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Shadow IT table">
            <Table>
              <TableCaption className="sr-only">Shadow IT apps, {orderText}{anything ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                {header("display_name")}
                <TableHead scope="col">Catalog</TableHead>
                {header("users_30d")}
                {header("sign_ins_30d")}
                {header("consenting_users")}
                <TableHead scope="col">Scopes</TableHead>
                {header("first_seen_at")}
                <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <ShadowRow key={r.id} r={r} write={write} busy={opening === r.id} onDecide={(mode) => void decide(r, mode)} showConnector={shadowConnectors.length > 1} />
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <nav aria-label="Pages" className="mt-3 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span className="mr-auto">{plural(rows.length, "app")} on this page, sorted {orderText}. Users are estimated; sign-ins are successful interactive sign-ins.</span>
            <Button size="sm" variant="outline" disabled={!q.data?.previous} onClick={() => f.setCursor(q.data?.previous ?? null)}>Previous</Button>
            <Button size="sm" variant="outline" disabled={!q.data?.next} onClick={() => f.setCursor(q.data?.next ?? null)}>Next</Button>
          </nav>
        </>
      )}
      {deciding && (
        <DecisionDialog mode={deciding.mode} obs={deciding.obs} onClose={() => setDeciding(null)}
          onDone={(text) => { setDeciding(null); setNotice(text); void invalidate("discovery/shadow-it", "discovery"); }} />
      )}
    </>
  );
}

function ShadowRow({ r, write, busy, onDecide, showConnector }: { r: Row; write: boolean; busy: boolean; onDecide: (mode: Mode) => void; showConnector: boolean }) {
  const decidable = r.status === "new" && r.candidate && (r.candidate.status === "new" || r.candidate.status === "changed");
  return (
    <TableRow className="align-top" data-app={r.name}>
      <TableHead scope="row" className="min-w-44 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
        {r.name}
        <span className="block text-sm font-normal text-muted-foreground">
          {r.verified_publisher ? <>Verified publisher: {r.verified_publisher}</> : r.publisher ? <>Publisher: {r.publisher} (not verified)</> : "Publisher not given"}
        </span>
        {showConnector && <span className="block text-sm font-normal text-muted-foreground">From {r.connector.name}</span>}
      </TableHead>
      <TableCell className="min-w-36 whitespace-normal">
        <StatusChip status={r.status} />
        {r.record && <span className="mt-1 block text-sm"><RecordLink record={r.record} className="underline" /></span>}
      </TableCell>
      <TableCell className="tabular">{r.users_30d.toLocaleString("en-US")}<span className="sr-only"> users (estimated)</span></TableCell>
      <TableCell className="whitespace-normal">
        <span className="tabular">{r.sign_ins_30d.toLocaleString("en-US")}</span>
        <span className="block text-sm text-muted-foreground">{r.last_sign_in_at ? <>Last <span className="tabular">{day(r.last_sign_in_at)}</span></> : "No sign-in read"}</span>
      </TableCell>
      <TableCell className="min-w-36 whitespace-normal"><ConsentText r={r} /></TableCell>
      <TableCell className="min-w-56 max-w-sm whitespace-normal"><Scopes r={r} /></TableCell>
      <TableCell className="whitespace-normal">
        <span className="tabular">{day(r.first_seen_at)}</span>
        <span className="block text-sm text-muted-foreground">Seen <span className="tabular">{day(r.last_seen_at)}</span></span>
      </TableCell>
      <TableCell className="whitespace-normal text-right">
        {write && decidable && (
          <div className="flex flex-wrap justify-end gap-1.5">
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide("create")}>Create record<span className="sr-only"> for {r.name}</span></Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide("link")}>Link to record<span className="sr-only">: {r.name}</span></Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDecide("dismiss")}>Dismiss<span className="sr-only"> {r.name}</span></Button>
          </div>
        )}
        {r.status === "new" && !r.candidate && <span className="text-sm text-muted-foreground">No candidate yet; the next run adds it.</span>}
        {r.status === "new" && r.candidate?.status === "out_of_scope" && <span className="text-sm text-muted-foreground">Out of scope in {r.candidate.connector.name}.</span>}
        {r.status === "dismissed" && r.candidate && <Link to="/discovery?status=dismissed" className="text-sm underline">Review in Discovery<span className="sr-only">: {r.name}</span></Link>}
      </TableCell>
    </TableRow>
  );
}
