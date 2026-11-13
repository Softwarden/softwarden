/** Discovery › Run history: every connector's runs, newest first, from `discovery/runs` (keyset
 *  pages), filtered in the URL by status, trigger, connector and the day they were queued (organization timezone); and
 *  one run's page with its timings, counts, skipped breakdown, notes and error. Every role reads them. A queued or
 *  running run's page asks again every few seconds. Loaded on first use. */
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { when, type Connector, type ConnectorRun, type Page } from "@/api";
import { Breadcrumb, EmptyState, ErrorBanner, isNotFound, Loading, NotFound, PageHead, Select, TableRegion } from "@/components/app";
import { DetailList, DetailRow } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { problemText, useApiQuery } from "@/lib/query";
import {
  changesText, durationText, RunResult, RUN_STATUSES, RunStatusChip, runStatusLabel, seenKept, skippedText, TRIGGER_LABEL,
} from "@/features/discovery/discovery-ui";

const FILTERS = ["status", "trigger", "connector", "from", "to"] as const;
type FilterKey = (typeof FILTERS)[number];

export function RunHistoryPage() {
  const [params, setParams] = useSearchParams();
  const value = (k: FilterKey) => params.get(k) ?? "";
  const cursor = params.get("cursor") ?? "";
  const request = new URLSearchParams();
  for (const k of FILTERS) if (value(k)) request.set(k, value(k));
  if (cursor) request.set("cursor", cursor);
  const query = request.toString();
  const q = useApiQuery<Page<ConnectorRun>>(`discovery/runs${query ? `?${query}` : ""}`);
  const connectors = useApiQuery<Connector[]>("connectors");
  const filtered = FILTERS.some((k) => value(k));
  // A cursor only works for the filters it was made with, so any filter change starts at the first page.
  // A discrete change (a select, Clear) pushes a history entry so Back undoes it; a typed one (a date box) replaces.
  const setFilter = (k: FilterKey, v: string, typed = false) => setParams((p) => {
    const n = new URLSearchParams(p);
    if (v) n.set(k, v); else n.delete(k);
    n.delete("cursor");
    return n;
  }, { replace: typed });
  const setCursor = (c: string | null) => setParams((p) => { const n = new URLSearchParams(p); if (c) n.set("cursor", c); else n.delete("cursor"); return n; });
  const clear = () => setParams({});
  const rows = q.data?.results;
  const connectorOptions = (connectors.data ?? []).map((c) => ({ value: c.id, label: c.name }));
  if (value("connector") && !connectorOptions.some((o) => o.value === value("connector"))) {
    connectorOptions.push({ value: value("connector"), label: connectors.data ? "Unknown connector" : "Loading connector…" });
  }
  const select = (k: FilterKey, label: string, options: { value: string; label: string }[], all = "All") => (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={`runs-${k}`}>{label}</Label>
      <Select id={`runs-${k}`} className="w-48" value={value(k)} onChange={(e) => setFilter(k, e.target.value)}>
        <option value="">{all}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Select>
    </div>
  );
  return (
    <>
      <Breadcrumb trail={[{ to: "/discovery", label: "Discovery" }]} current="Run history" />
      <PageHead title="Run history" desc="Every discovery run, newest first: when it was queued, started and finished, who started it, what it counted and what its scope skipped." />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the run history" onSubmit={(e) => e.preventDefault()}>
        {select("status", "Status", RUN_STATUSES.map((s) => ({ value: s, label: runStatusLabel(s) })))}
        {select("trigger", "Trigger", (Object.keys(TRIGGER_LABEL) as (keyof typeof TRIGGER_LABEL)[]).map((t) => ({ value: t, label: TRIGGER_LABEL[t] })))}
        {select("connector", "Connector", connectorOptions, "All connectors")}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="runs-from">Queued from</Label>
          <Input id="runs-from" type="date" className="w-44" value={value("from")} onChange={(e) => setFilter("from", e.target.value, true)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="runs-to">Queued to</Label>
          <Input id="runs-to" type="date" className="w-44" value={value("to")} onChange={(e) => setFilter("to", e.target.value, true)} />
        </div>
        {filtered && <Button type="button" variant="outline" onClick={clear}>Clear filters</Button>}
      </form>
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={problemText(q.error, "Couldn't load the run history.")} onRetry={() => void q.refetch()} />
          {filtered && <Button variant="outline" onClick={clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the run history" rows={5} />}
      {rows && rows.length === 0 && (filtered
        ? <EmptyState title="No runs match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>Try another status or a wider date range.</EmptyState>
        : <EmptyState title="No runs yet" action={<Button asChild variant="outline"><Link to="/discovery">Go to Discovery</Link></Button>}>A run appears here once someone presses Run now on a connector, or its daily schedule runs it.</EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Run history table">
            <Table>
              <TableCaption className="sr-only">Discovery runs, newest first{filtered ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Queued</TableHead><TableHead scope="col">Connector</TableHead><TableHead scope="col">Status</TableHead>
                <TableHead scope="col">Trigger</TableHead><TableHead scope="col">Duration</TableHead><TableHead scope="col">Seen</TableHead>
                <TableHead scope="col">Skipped</TableHead><TableHead scope="col">Changes</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.id} className="align-top" data-status={r.status}>
                    <TableHead scope="row" className="font-normal text-foreground">
                      <Link to={`/discovery/runs/${r.id}`} className="tabular font-semibold underline">{when(r.queued_at ?? r.started_at)}<span className="sr-only">, {r.connector.name}, {runStatusLabel(r.status)}</span></Link>
                    </TableHead>
                    <TableCell className="whitespace-normal"><Link to={`/discovery/connectors/${r.connector.id}`}>{r.connector.name}</Link></TableCell>
                    <TableCell className="whitespace-normal">
                      <RunStatusChip status={r.status} />
                      {r.error && <span className="mt-1 block max-w-xs text-sm text-error-foreground [overflow-wrap:anywhere]">{r.error}</span>}
                    </TableCell>
                    <TableCell className="whitespace-normal">{TRIGGER_LABEL[r.trigger]}{r.requested_by && <span className="block text-sm text-muted-foreground">by {r.requested_by.name}</span>}</TableCell>
                    <TableCell className="tabular">{durationText(r.duration_seconds)}</TableCell>
                    <TableCell className="tabular">{seenKept(r)}</TableCell>
                    <TableCell className="max-w-xs whitespace-normal text-sm">{skippedText(r)}</TableCell>
                    <TableCell className="tabular">{changesText(r)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <nav aria-label="Pages" className="mt-3 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span className="mr-auto">{rows.length} {rows.length === 1 ? "run" : "runs"} on this page, newest first.</span>
            <Button size="sm" variant="outline" disabled={!q.data?.previous} onClick={() => setCursor(q.data?.previous ?? null)}>Previous</Button>
            <Button size="sm" variant="outline" disabled={!q.data?.next} onClick={() => setCursor(q.data?.next ?? null)}>Next</Button>
          </nav>
        </>
      )}
    </>
  );
}

const live = (r: ConnectorRun | undefined) => !!r && (r.status === "queued" || r.status === "running");

/** The old address, /discovery/runs/{connector}/{run}, kept for links made before the run had its own. */
export function OldRunDetailRedirect() {
  const { runId = "" } = useParams();
  return <Navigate to={`/discovery/runs/${runId}`} replace />;
}

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const q = useApiQuery<ConnectorRun>(`discovery/runs/${runId}`, { refetchInterval: (query) => (live(query.state.data as ConnectorRun | undefined) ? 3000 : false) });
  const r = q.data;
  const trail = [{ to: "/discovery", label: "Discovery" }, { to: "/discovery/runs", label: "Run history" }];
  if (q.isError && isNotFound(q.error)) return <NotFound what="run" back={{ to: "/discovery/runs", label: "Run history" }} trail={trail} />;
  if (q.isError) {
    return (
      <>
        <Breadcrumb trail={trail} current="Run" />
        <PageHead title="Run" />
        <ErrorBanner message={problemText(q.error, "Couldn't load the run.")} onRetry={() => void q.refetch()} />
      </>
    );
  }
  if (!r) return <Loading what="the run" rows={3} />;
  const title = `Run of ${r.connector.name}, ${when(r.queued_at ?? r.started_at)}`;
  return (
    <>
      <Breadcrumb trail={trail} current={when(r.queued_at ?? r.started_at)} />
      <PageHead title={title} desc={<span className="inline-flex flex-wrap items-center gap-2"><RunStatusChip status={r.status} />{live(r) && "This page updates on its own while the run is queued or running."}</span>} />
      <section aria-labelledby="run-facts" className="mb-5 max-w-3xl rounded-lg border bg-card">
        <h2 id="run-facts" className="border-b px-4 py-2.5 font-display text-section font-semibold">When and who</h2>
        <DetailList>
          <DetailRow label="Connector"><Link to={`/discovery/connectors/${r.connector.id}`}>{r.connector.name}</Link></DetailRow>
          <DetailRow label="Trigger">{TRIGGER_LABEL[r.trigger]}{r.requested_by ? `, by ${r.requested_by.name}` : ""}</DetailRow>
          <DetailRow label="Queued"><span className="tabular">{when(r.queued_at)}</span></DetailRow>
          <DetailRow label="Started"><span className="tabular">{r.started_at ? when(r.started_at) : r.status === "queued" ? "Not yet" : "Never started"}</span></DetailRow>
          <DetailRow label="Finished"><span className="tabular">{r.finished_at ? when(r.finished_at) : live(r) ? "Not yet" : "—"}</span></DetailRow>
          <DetailRow label="Duration"><span className="tabular">{durationText(r.duration_seconds)}</span></DetailRow>
        </DetailList>
      </section>
      {/* One live region that stays mounted, so the change from queued to running to finished is announced. */}
      <p className="sr-only" aria-live="polite" data-testid="run-live">{r.status === "queued" ? "Queued." : r.status === "running" ? "Running." : `Run ${r.status === "succeeded" ? "finished" : "failed"}.`}</p>
      {live(r)
        ? <p className="text-muted-foreground">{r.status === "queued" ? "Waiting for the worker; it starts the run within a minute." : "Running. The counts appear when it finishes."}</p>
        : <section aria-labelledby="run-numbers" className="max-w-3xl"><h2 id="run-numbers" className="mb-2 font-display text-section font-semibold">What it found</h2><RunResult run={r} /></section>}
    </>
  );
}
