/** SSO coverage (`/discovery/sso-coverage`, capability `discovery.entra`, every role reads): one row per
 *  live application with whether it is behind single sign-on, whether SCIM provisioning is on and whether conditional
 *  access requires MFA, from the `entra_coverage` source (GET reports/sso-coverage, keyset pages, most critical first).
 *  Each dimension is a chip with a glyph and words, with the API's reason under it; unknown is a named gap with its
 *  reason. Where the record's own SSO or SCIM field says otherwise, a "Source differs" chip names it (shown, never
 *  applied: the record is edited as any record). Filters, sort and the page cursor live in the URL; Export CSV gives way
 *  to a sentence while platform staff impersonate (the API refuses it). Loaded on first use. */
import { useState } from "react";
import { Link } from "react-router-dom";
import type { Connector } from "@/api";
import { EmptyState, ErrorBanner, Loading, PageHead, TableRegion } from "@/components/app";
import { RecordLink } from "@/components/record-link";
import { chip, CriticalityMark, GapChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { CommitInput, ExportCsvButton, filterProblem, SelectFilter, SortButton, Unavailable, useUrlFilters } from "@/components/url-filters";
import { orgDate } from "@/components/org-banners";
import { useTypedQuery, type QueryOf } from "@/lib/openapi";
import { problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import { CoverageChip, GAP_CHOICES, lookOf, MFA, MFA_CHOICES, SCIM, SCIM_CHOICES, SSO, SSO_CHOICES, yesNo, type CoverageRow } from "./coverage-ui";

type Query = QueryOf<"reports/sso-coverage", "get">;

const FILTERS = ["sso", "scim", "mfa", "criticality", "gap", "linked", "q"] as const;
const CRITICALITY_CHOICES = [
  { value: "critical", label: "Critical" }, { value: "high", label: "High" }, { value: "medium", label: "Medium" },
  { value: "low", label: "Low" }, { value: "none", label: "No criticality" },
];
const LINKED_CHOICES = [{ value: "true", label: "Matched to an Entra app" }, { value: "false", label: "Not matched" }];
/** Sortable columns. Ascending is the API's order of meaning: most critical first, worst coverage first. */
const SORTS = {
  name: { label: "Application", asc: "A to Z", desc: "Z to A" },
  criticality: { label: "Criticality", asc: "most critical first", desc: "least critical first" },
  sso: { label: "Single sign-on", asc: "worst first", desc: "best first" },
  scim: { label: "SCIM", asc: "worst first", desc: "best first" },
  mfa: { label: "MFA", asc: "worst first", desc: "best first" },
} as const;
type SortKey = keyof typeof SORTS;
const DEFAULT_ORDER = "criticality";

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;

export function SsoCoveragePage() {
  const me = useMe();
  if (!me.capabilities.includes("discovery.entra")) {
    return <><PageHead title="SSO coverage" /><Unavailable what="SSO coverage" reason="It needs the Microsoft Entra discovery source, which this organization's edition doesn't include." testId="coverage-unavailable" /></>;
  }
  return <SsoCoverage />;
}

function SsoCoverage() {
  const me = useMe();
  const f = useUrlFilters(FILTERS);
  const ordering = f.value("ordering") || DEFAULT_ORDER;
  const cursor = f.value("cursor");
  const query: Record<string, string> = { ...f.query };
  const exportSearch = new URLSearchParams(query).toString();
  if (ordering !== DEFAULT_ORDER) query.ordering = ordering;
  const q = useTypedQuery("reports/sso-coverage", { query: { ...query, ...(cursor ? { cursor } : {}) } as Partial<Query> });
  const connectors = useApiQuery<Connector[]>("connectors");
  const coverageConnectors = (connectors.data ?? []).filter((c) => c.source === "entra_coverage");
  const [notice, setNotice] = useState("");
  const rows = q.data?.results;
  const tz = me.organization.timezone;

  const sortBy = (key: SortKey) => {
    const active = ordering.replace(/^-/, "") === key;
    const next = active && !ordering.startsWith("-") ? `-${key}` : key;
    f.set("ordering", next === DEFAULT_ORDER ? "" : next);
  };
  const header = (key: SortKey) => {
    const active = ordering.replace(/^-/, "") === key;
    const descending = active && ordering.startsWith("-");
    const nextDescending = active && !descending;
    return (
      <TableHead scope="col" aria-sort={active ? (descending ? "descending" : "ascending") : undefined}>
        <SortButton label={SORTS[key].label} active={active} descending={descending} onClick={() => sortBy(key)} next={nextDescending ? SORTS[key].desc : SORTS[key].asc} />
      </TableHead>
    );
  };
  const orderText = (() => {
    const key = (ordering.replace(/^-/, "") in SORTS ? ordering.replace(/^-/, "") : DEFAULT_ORDER) as SortKey;
    const label = key === "name" ? "name" : key === "sso" || key === "criticality" ? SORTS[key].label.toLowerCase() : SORTS[key].label;
    return `by ${label}, ${ordering.startsWith("-") ? SORTS[key].desc : SORTS[key].asc}`;
  })();
  const noConnector = connectors.data && coverageConnectors.length === 0;
  const neverRun = coverageConnectors.length > 0 && coverageConnectors.every((c) => !c.last_run);

  return (
    <>
      <PageHead title="SSO coverage" desc="Every live application: whether it is behind single sign-on, whether SCIM provisioning is on, and whether conditional access requires MFA, as Microsoft Entra ID says.">
        <ExportCsvButton path={`reports/sso-coverage/export.csv${exportSearch ? `?${exportSearch}` : ""}`} fallback="softwarden-sso-coverage.csv" what="the SSO coverage report" onDone={setNotice} />
      </PageHead>
      <SuccessNote text={notice} />
      {noConnector && (
        <p className="mb-4 max-w-prose rounded-md border border-info-border bg-info-bg px-3 py-2 text-sm" data-testid="no-coverage-source">
          <span aria-hidden="true">ⓘ </span><span className="font-semibold">No SSO and MFA coverage source is set up,</span> so every application's coverage is unknown.{" "}
          {me.role === "admin" ? <Link className="underline" to="/discovery/setup/entra_coverage">Set up Microsoft Entra SSO and MFA coverage</Link> : "An Admin sets it up in Discovery."}
        </p>
      )}
      {neverRun && (
        <p className="mb-4 max-w-prose rounded-md border border-info-border bg-info-bg px-3 py-2 text-sm" data-testid="coverage-never-run">
          <span aria-hidden="true">ⓘ </span>The coverage source is set up but hasn't finished a run; coverage shows after the first run.
        </p>
      )}
      {connectors.isError && <ErrorBanner message={problemText(connectors.error, "Couldn't load the discovery connectors, so this page can't say whether the coverage source is set up.")} onRetry={() => void connectors.refetch()} />}
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter SSO coverage" onSubmit={(e) => e.preventDefault()}>
        <SelectFilter id="cov-sso" label="Single sign-on" value={f.value("sso")} onChange={(v) => f.set("sso", v)} options={SSO_CHOICES} unknown="Several modes" className="w-48" />
        <SelectFilter id="cov-scim" label="SCIM" value={f.value("scim")} onChange={(v) => f.set("scim", v)} options={SCIM_CHOICES} unknown="Several states" className="w-40" />
        <SelectFilter id="cov-mfa" label="MFA" value={f.value("mfa")} onChange={(v) => f.set("mfa", v)} options={MFA_CHOICES} unknown="Several results" className="w-56" />
        <SelectFilter id="cov-gap" label="Gap" value={f.value("gap")} onChange={(v) => f.set("gap", v)} options={GAP_CHOICES} unknown="Several gaps" all="With or without gaps" className="w-52" />
        <SelectFilter id="cov-criticality" label="Criticality" value={f.value("criticality")} onChange={(v) => f.set("criticality", v)} options={CRITICALITY_CHOICES} unknown="Several criticalities" className="w-44" />
        <SelectFilter id="cov-linked" label="Entra app" value={f.value("linked")} onChange={(v) => f.set("linked", v)} options={LINKED_CHOICES} all="Matched or not" className="w-48" />
        <CommitInput id="cov-q" label="Search" type="search" value={f.value("q")} onChange={(v) => f.set("q", v)} placeholder="Name or Entra application ID" />
        {(f.filtered || f.value("ordering")) && <Button type="button" variant="outline" onClick={f.clear}>Clear filters</Button>}
      </form>
      <p className="sr-only" aria-live="polite">{rows ? `${plural(rows.length, "application")} on this page.` : ""}</p>
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={filterProblem(q.error, "Couldn't load the SSO coverage report.")} onRetry={() => void q.refetch()} />
          {f.filtered && <Button variant="outline" onClick={f.clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the SSO coverage report" rows={5} />}
      {rows && rows.length === 0 && (f.filtered
        ? <EmptyState title="No applications match these filters" action={<Button variant="outline" onClick={f.clear}>Clear filters</Button>}>Try another gap or coverage value, or search for another name.</EmptyState>
        : <EmptyState title="No live applications" action={me.role !== "viewer" && <Button asChild variant="outline"><Link to="/applications/new">Add application</Link></Button>}>
            The report lists every application that isn't archived or retired, with its coverage from Microsoft Entra ID.
          </EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="SSO coverage table">
            <Table>
              <TableCaption className="sr-only">SSO and MFA coverage of each application, {orderText}{f.filtered ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                {header("name")}
                {header("criticality")}
                {header("sso")}
                {header("scim")}
                {header("mfa")}
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((r) => <CoverageTableRow key={r.record.id} r={r} tz={tz} />)}
              </TableBody>
            </Table>
          </TableRegion>
          <nav aria-label="Pages" className="mt-3 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span className="mr-auto">{plural(rows.length, "application")} on this page, sorted {orderText}. Unknown is never counted as no SSO.</span>
            <Button size="sm" variant="outline" disabled={!q.data?.previous} onClick={() => f.setCursor(q.data?.previous ?? null)}>Previous</Button>
            <Button size="sm" variant="outline" disabled={!q.data?.next} onClick={() => f.setCursor(q.data?.next ?? null)}>Next</Button>
          </nav>
        </>
      )}
    </>
  );
}

function CoverageTableRow({ r, tz }: { r: CoverageRow; tz: string }) {
  return (
    <TableRow className="align-top" data-app={r.record.name}>
      <TableHead scope="row" className="min-w-48 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
        <RecordLink record={r.record} className="underline" />
        {r.entra_app_id
          ? <span className="block text-sm font-normal text-muted-foreground">App ID <code className="font-mono text-xs">{r.entra_app_id}</code></span>
          : <span className="block text-sm font-normal text-muted-foreground">Not linked to an Entra app</span>}
        {r.differs.length > 0 && (
          <span className="mt-1 block font-normal" data-testid="row-differs">
            <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Source differs</span>
            {r.differs.map((d) => (
              <span key={d.field} className="block text-sm">{d.label}: {yesNo(d.source)} in Entra, {yesNo(d.curated).toLowerCase()} here</span>
            ))}
          </span>
        )}
      </TableHead>
      <TableCell className="whitespace-normal">{r.criticality ? <CriticalityMark value={r.criticality} /> : <GapChip label="No criticality" />}</TableCell>
      <Dimension look={lookOf(SSO, r.sso)} detail={r.sso_detail} extra={r.sso_mode && r.sso !== "unknown" ? `Entra mode: ${r.sso_mode}` : ""} testId="cell-sso" />
      <Dimension look={lookOf(SCIM, r.scim)} detail={r.scim_detail} extra={r.scim_checked_at ? `Checked ${orgDate(r.scim_checked_at, tz)}` : ""} testId="cell-scim" />
      <Dimension look={lookOf(MFA, r.mfa)} detail={r.mfa_detail}
        extra={[r.policies.length ? plural(r.policies.length, "matching policy", "matching policies") : "", r.mfa_checked_at ? `checked ${orgDate(r.mfa_checked_at, tz)}` : ""].filter(Boolean).join(", ")} testId="cell-mfa" />
    </TableRow>
  );
}

function Dimension({ look, detail, extra, testId }: { look: Parameters<typeof CoverageChip>[0]["look"]; detail: string; extra: string; testId: string }) {
  return (
    <TableCell className="min-w-48 max-w-xs whitespace-normal" data-testid={testId}>
      <CoverageChip look={look} />
      {detail && <span className="mt-1 block text-sm text-muted-foreground [overflow-wrap:anywhere]">{detail}</span>}
      {extra && <span className="block text-sm text-muted-foreground">{extra.charAt(0).toUpperCase() + extra.slice(1)}</span>}
    </TableCell>
  );
}
