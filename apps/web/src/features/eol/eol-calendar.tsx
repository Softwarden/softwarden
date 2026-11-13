/** The end-of-life calendar (capability `eol`, `/end-of-life`, every role reads): every tracked
 *  version of the live catalog from GET /end-of-life, soonest end of life first, as a timeline by month (a jump list of
 *  months with their counts, and one table whose rows are grouped under each month). Filters in the URL: the window
 *  (`window`: 90, 183 or 365 days, or `past`, sent as the API's `days`, which always includes what is already past),
 *  `status`, `product`, `kind`, `criticality` and `owner` (business or technical). Export CSV sends the same filters.
 *  Loaded on first use. */
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading, PageHead, TableRegion } from "@/components/app";
import { RecordLink } from "@/components/record-link";
import { ExportCsvButton, filterProblem, SelectFilter, Unavailable, useUrlFilters, type Option } from "@/components/url-filters";
import { orgDate } from "@/components/org-banners";
import { chip, CriticalityMark, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ExtLink } from "@/features/catalog/detail";
import { KIND_LABEL, isCatalogKind } from "@/lib/kinds";
import { useTypedQuery, type QueryOf } from "@/lib/openapi";
import { canWrite, useMe } from "@/lib/session";
import { eolDetail, EolStatusChip, type TrackedVersion } from "./eol-ui";

const FILTERS = ["window", "status", "product", "kind", "criticality", "owner"] as const;
/** The window choices and the `days` each sends; every one includes versions already past end of life. */
const WINDOWS: (Option & { days: string })[] = [
  { value: "90", label: "Past, or ending within 90 days", days: "90" },
  { value: "183", label: "Past, or ending within 6 months", days: "183" },
  { value: "365", label: "Past, or ending within 12 months", days: "365" },
  { value: "past", label: "Already past end of life", days: "0" },
];
const STATUSES: Option[] = [
  { value: "end_of_life", label: "End of life" }, { value: "security_only", label: "Security fixes only" },
  { value: "supported", label: "Supported" }, { value: "unknown", label: "Unknown version" },
];
const KINDS: Option[] = [{ value: "application", label: "Applications" }, { value: "component", label: "Components" }];
const CRITICALITIES: Option[] = [
  { value: "critical", label: "Critical" }, { value: "high", label: "High" }, { value: "medium", label: "Medium" }, { value: "low", label: "Low" },
  { value: "none", label: "No criticality" },
];
type Query = QueryOf<"end-of-life", "get">;

export function EndOfLifePage() {
  const me = useMe();
  if (!me.capabilities.includes("eol")) return <><PageHead title="End of life" /><Unavailable what="End-of-life tracking" testId="eol-unavailable" /></>;
  return <Calendar />;
}

/** Bucket of a row on the timeline: "past" (end of life, with or without a date), "YYYY-MM", or "none" (no date). */
function bucketOf(r: TrackedVersion): string {
  if (r.status === "end_of_life" || (r.days_remaining !== null && r.days_remaining < 0)) return "past";
  return r.eol_date ? r.eol_date.slice(0, 7) : "none";
}
const MONTH = new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" });
const bucketLabel = (b: string) => (b === "past" ? "Past end of life" : b === "none" ? "No end-of-life date" : MONTH.format(new Date(`${b}-01T00:00:00Z`)));

function Calendar() {
  const me = useMe();
  const f = useUrlFilters(FILTERS);
  const win = WINDOWS.find((w) => w.value === f.value("window"));
  const query: Partial<Query> = {
    ...(win ? { days: Number(win.days) } : {}),
    ...(f.value("status") ? { status: f.value("status") } : {}),
    ...(f.value("product") ? { product: f.value("product") } : {}),
    ...(f.value("kind") ? { kind: f.value("kind") as Query["kind"] } : {}),
    ...(f.value("criticality") ? { criticality: f.value("criticality") } : {}),
    ...(f.value("owner") ? { owner: f.value("owner") } : {}),
  };
  const q = useTypedQuery("end-of-life", { query });
  // The products to filter by: every tracked product, from the unfiltered overview (the same cached request when no
  // filter is set).
  const all = useTypedQuery("end-of-life");
  const [notice, setNotice] = useState("");
  const byLabel = (a: Option, b: Option) => a.label.localeCompare(b.label);
  const products: Option[] = [...new Map((all.data?.results ?? []).map((r) => [r.product, r.product_label])).entries()]
    .map(([value, label]) => ({ value, label })).sort(byLabel);
  // The people who own a tracked record, business or technical.
  const owners: Option[] = [...new Map((all.data?.results ?? []).flatMap((r) => [r.record.business_owner, r.record.technical_owner])
    .filter((p): p is NonNullable<typeof p> => !!p).map((p) => [p.id, p.name])).entries()]
    .map(([value, label]) => ({ value, label })).sort(byLabel);
  const search = new URLSearchParams(Object.entries(query).map(([k, v]) => [k, String(v)])).toString();
  const rows = q.data?.results;
  // One group per bucket, whatever order the rows come in: past first, then the months in order, no date last.
  const buckets = new Map<string, TrackedVersion[]>();
  for (const r of rows ?? []) buckets.set(bucketOf(r), [...(buckets.get(bucketOf(r)) ?? []), r]);
  const rank = (b: string) => (b === "past" ? "0" : b === "none" ? "2" : `1${b}`);
  const groups = [...buckets.entries()].sort(([a], [b]) => rank(a).localeCompare(rank(b))).map(([bucket, list]) => ({ bucket, rows: list }));
  const cache = q.data?.cache;
  return (
    <>
      <PageHead title="End of life" desc="End-of-life and end-of-support dates of the versions the catalog runs, from endoflife.date, soonest first.">
        <ExportCsvButton path={`end-of-life/export.csv${search ? `?${search}` : ""}`} fallback="softwarden-end-of-life.csv" what="the end-of-life calendar" onDone={setNotice} />
      </PageHead>
      <SuccessNote text={notice} />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the end-of-life calendar" onSubmit={(e) => e.preventDefault()}>
        <SelectFilter id="eol-window" label="Window" value={f.value("window")} onChange={(v) => f.set("window", v)} options={WINDOWS} all="Any date" className="w-64" />
        <SelectFilter id="eol-status" label="Status" value={f.value("status")} onChange={(v) => f.set("status", v)} options={STATUSES} all="Any status" className="w-48" />
        <SelectFilter id="eol-product" label="Product" value={f.value("product")} onChange={(v) => f.set("product", v)} options={products} all="Every product" className="w-48" />
        <SelectFilter id="eol-kind" label="Kind" value={f.value("kind")} onChange={(v) => f.set("kind", v)} options={KINDS} all="Applications and components" className="w-56" />
        <SelectFilter id="eol-criticality" label="Criticality" value={f.value("criticality")} onChange={(v) => f.set("criticality", v)} options={CRITICALITIES} all="Any criticality" unknown="Several criticalities" className="w-44" />
        <SelectFilter id="eol-owner" label="Owner" value={f.value("owner")} onChange={(v) => f.set("owner", v)} options={owners} all="Anyone" unknown="A person who owns no tracked record" className="w-52" />
        {f.filtered && <Button type="button" variant="outline" onClick={f.clear}>Clear filters</Button>}
      </form>
      {cache && <CacheNote cache={cache} timeZone={me.organization.timezone} />}
      <p className="sr-only" aria-live="polite">{rows ? `${rows.length} ${rows.length === 1 ? "version" : "versions"} shown.` : ""}</p>
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={filterProblem(q.error, "Couldn't load the end-of-life calendar.")} onRetry={() => void q.refetch()} />
          {f.filtered && <Button variant="outline" onClick={f.clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the end-of-life calendar" rows={5} />}
      {rows && rows.length === 0 && (f.filtered
        ? <EmptyState title="No versions match these filters" action={<Button variant="outline" onClick={f.clear}>Clear filters</Button>}>Try a longer window, another status or every product.</EmptyState>
        : <EmptyState title="No versions tracked yet" action={<Button asChild variant="outline"><Link to="/applications">Open Applications</Link></Button>}>
            {canWrite(me)
              ? "Open an application or component and link it to its endoflife.date product in the End of life panel of its Overview."
              : "Editors and Admins link applications and components to their endoflife.date product; tracked versions show here."}
          </EmptyState>)}
      {rows && rows.length > 0 && q.data && (
        <>
          <Counts counts={q.data.counts} />
          <nav aria-label="Timeline" className="mb-3">
            <ol className="flex flex-wrap gap-2 text-sm">
              {groups.map((g) => (
                <li key={g.bucket}>
                  <a href={`#eol-${g.bucket}`} className={chip({ state: g.bucket === "past" ? "overdue" : "neutral" })}>
                    {g.bucket === "past" && <span aria-hidden="true">!</span>}{bucketLabel(g.bucket)} <span className="tabular">{g.rows.length}</span>
                  </a>
                </li>
              ))}
            </ol>
          </nav>
          <TableRegion label="End-of-life calendar table">
            <Table>
              <TableCaption className="sr-only">Tracked versions by end-of-life date, soonest first, grouped by month{f.filtered ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Record</TableHead><TableHead scope="col">Product and version</TableHead><TableHead scope="col">Status</TableHead>
                <TableHead scope="col">End of life</TableHead><TableHead scope="col">Support</TableHead><TableHead scope="col">Latest release</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {groups.map((g) => (
                  <Fragment key={g.bucket}>
                    <TableRow className="bg-table-head hover:bg-table-head">
                      <TableHead colSpan={6} scope="colgroup" id={`eol-${g.bucket}`} tabIndex={-1} className="font-semibold text-foreground">
                        {bucketLabel(g.bucket)} <span className="font-normal text-muted-foreground">({g.rows.length})</span>
                      </TableHead>
                    </TableRow>
                    {g.rows.map((r) => <CalendarRow key={r.id} r={r} />)}
                  </Fragment>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <p className="mt-2 text-sm text-muted-foreground">{rows.length} {rows.length === 1 ? "version" : "versions"}, by end-of-life date (soonest first; versions with no date last). Archived and retired records aren't listed.</p>
        </>
      )}
    </>
  );
}

function Counts({ counts }: { counts: { end_of_life: number; security_only: number; supported: number; unknown: number } }) {
  return (
    <ul aria-label="Versions by status" className="mb-3 flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
      <li>End of life <span className="tabular font-semibold text-foreground">{counts.end_of_life}</span></li>
      <li>Security fixes only <span className="tabular font-semibold text-foreground">{counts.security_only}</span></li>
      <li>Supported <span className="tabular font-semibold text-foreground">{counts.supported}</span></li>
      <li>Unknown <span className="tabular font-semibold text-foreground">{counts.unknown}</span></li>
    </ul>
  );
}

function CacheNote({ cache, timeZone }: { cache: { enabled: boolean; products: number; last_success_at: string | null; last_error: string | null }; timeZone: string }) {
  if (cache.last_error) {
    return <p className="mb-4 rounded-md border border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground"><span aria-hidden="true">◐ </span>The last refresh from endoflife.date failed: {cache.last_error} The dates shown are from the last good refresh.</p>;
  }
  if (!cache.enabled && cache.products === 0) {
    return <p className="mb-4 rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm text-muted-foreground"><span aria-hidden="true">⊘ </span>Downloads from endoflife.date are off on this installation and no snapshot is imported, so no dates are known yet.</p>;
  }
  return <p className="mb-4 text-sm text-muted-foreground">Dates from endoflife.date{cache.last_success_at ? `, last refreshed ${orgDate(cache.last_success_at, timeZone)}` : ""}.</p>;
}

function CalendarRow({ r }: { r: TrackedVersion }) {
  const detail = eolDetail(r);
  const kind = isCatalogKind(r.record.kind) ? KIND_LABEL[r.record.kind] : r.record.kind;
  return (
    <TableRow className="align-top" data-eol-row={r.record.name}>
      <TableHead scope="row" className="min-w-44 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
        <RecordLink record={r.record} className="underline" />
        <span className="block text-sm font-normal text-muted-foreground">{kind}{r.instance ? `, instance ${r.instance.name}` : ""}</span>
        {r.record.criticality && <span className="block text-sm"><CriticalityMark value={r.record.criticality} suffix=" criticality" /></span>}
        {(r.record.business_owner || r.record.technical_owner) && (
          <span className="block text-sm font-normal text-muted-foreground">
            {[r.record.business_owner && `Business owner ${r.record.business_owner.name}`, r.record.technical_owner && `Technical owner ${r.record.technical_owner.name}`].filter(Boolean).join("; ")}
          </span>
        )}
      </TableHead>
      <TableCell className="min-w-48 whitespace-normal">
        <span className="block font-semibold">{r.product_label}{r.cycle ? ` ${r.cycle}` : ""}</span>
        <span className="block text-sm text-muted-foreground">
          {r.version ? `Version ${r.version}${r.version_source === "application" ? " (the application's Version field)" : ""}` : "No version recorded"}
          {r.cycle && r.cycle_source === "explicit" ? "; cycle set by hand" : ""}
        </span>
        {r.product_renamed_to && <span className={`${chip({ state: "pending" })} mt-1`}><span aria-hidden="true">◐</span>Renamed to {r.product_renamed_to} on endoflife.date</span>}
      </TableCell>
      <TableCell className="min-w-48 whitespace-normal">
        <EolStatusChip row={r} />
        {detail && <span className="mt-1 block text-sm text-muted-foreground">{detail}</span>}
        {r.status === "unknown" && <span className="mt-1 block max-w-xs text-sm text-muted-foreground">{r.why}</span>}
      </TableCell>
      <TableCell className="tabular">{r.eol_date ?? <span className="text-muted-foreground">No date</span>}</TableCell>
      <TableCell className="whitespace-normal text-sm">
        {r.support_until && <span className="block">Active support until <span className="tabular">{r.support_until}</span></span>}
        {r.extended_support_until && <span className="block">Extended support until <span className="tabular">{r.extended_support_until}</span></span>}
        {!r.support_until && !r.extended_support_until && <span className="text-muted-foreground">—</span>}
      </TableCell>
      <TableCell className="whitespace-normal text-sm">
        {r.latest ? <span className="tabular">{r.latest}</span> : <span className="text-muted-foreground">—</span>}
        {r.product_link && <span className="block"><ExtLink url={r.product_link} /></span>}
      </TableCell>
    </TableRow>
  );
}
