/** Vendor risk (capability `vendor_risk`, every role reads): the overview (`/vendor-risk`, GET
 *  vendors/risk-overview: every vendor with its tier, assurance, document statuses, next expiry and gaps, the riskiest
 *  first) and the documents register (`/vendor-risk/documents`, GET vendor-risk-documents in keyset pages, replaced
 *  documents hidden unless asked for). Filters live in the URL; each page exports what it shows as CSV through the
 *  shared download helper (audited; refused while a platform operator impersonates, which the helper says in words).
 *  Loaded on first use. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, ErrorBanner, Loading, PageHead, TableRegion } from "@/components/app";
import { SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { CommitInput, ExportCsvButton, filterProblem, SelectFilter, useUrlFilters } from "@/components/url-filters";
import { useTypedQuery, type QueryOf } from "@/lib/openapi";
import { useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import {
  AssuranceStatusChip, assuranceTypeLabel, daysText, DOCUMENT_KINDS, DocumentStatusChip, dpaLabel, GAP_CHOICES, kindLabel, ReplacedChip, riskPath,
  RiskGaps, TierMark, TIERS, VendorRiskSections, VendorRiskUnavailable, type RiskOverviewRow,
} from "./risk-ui";

const TIER_CHOICES = [...TIERS, { value: "none", label: "Not assessed" }];

// ---- the overview ------------------------------------------------------------------------------------------------------

const OVERVIEW_FILTERS = ["tier", "gap", "expiring_within", "expired"] as const;
type OverviewQuery = QueryOf<"vendors/risk-overview", "get">;

export function VendorRiskOverviewPage() {
  const me = useMe();
  if (!me.capabilities.includes("vendor_risk")) return <><PageHead title="Vendor risk" /><VendorRiskUnavailable /></>;
  return <Overview />;
}

function Overview() {
  const f = useUrlFilters(OVERVIEW_FILTERS);
  const query: Record<string, string> = {};
  for (const k of OVERVIEW_FILTERS) if (f.value(k)) query[k] = f.value(k);
  const search = new URLSearchParams(query).toString();
  const q = useTypedQuery("vendors/risk-overview", { query: query as Partial<OverviewQuery> });
  const [notice, setNotice] = useState("");
  const rows = q.data;
  return (
    <>
      <PageHead title="Vendor risk" desc="Every vendor's risk tier, assurance report, documents in force and gaps, the riskiest first.">
        <ExportCsvButton path={`vendors/risk-overview/export.csv${search ? `?${search}` : ""}`} fallback="softwarden-vendor-risk.csv" what="the vendor risk overview" onDone={setNotice} />
      </PageHead>
      <VendorRiskSections />
      <SuccessNote text={notice} />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the vendor risk overview" onSubmit={(e) => e.preventDefault()}>
        <SelectFilter id="risk-tier" label="Risk tier" value={f.value("tier")} onChange={(v) => f.set("tier", v)} options={TIER_CHOICES} />
        <SelectFilter id="risk-gap" label="Gap" value={f.value("gap")} onChange={(v) => f.set("gap", v)} options={GAP_CHOICES} />
        <CommitInput id="risk-expiring" label="Expiring within (days)" value={f.value("expiring_within")} onChange={(v) => f.set("expiring_within", v, true)} numeric className="w-36" placeholder="Days" />
        <SelectFilter id="risk-expired" label="Expired documents" value={f.value("expired")} onChange={(v) => f.set("expired", v)}
          options={[{ value: "true", label: "Has an expired document" }, { value: "false", label: "None expired" }]} />
        {f.filtered && <Button type="button" variant="outline" onClick={f.clear}>Clear filters</Button>}
      </form>
      <p className="sr-only" aria-live="polite">{rows ? `${rows.length} ${rows.length === 1 ? "vendor" : "vendors"} shown.` : ""}</p>
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={filterProblem(q.error, "Couldn't load the vendor risk overview.")} onRetry={() => void q.refetch()} />
          {f.filtered && <Button variant="outline" onClick={f.clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the vendor risk overview" rows={5} />}
      {rows && rows.length === 0 && (f.filtered
        ? <EmptyState title="No vendors match these filters" action={<Button variant="outline" onClick={f.clear}>Clear filters</Button>}>Try another tier or gap, or a longer expiry window.</EmptyState>
        : <EmptyState title="No vendors yet" action={<Button asChild variant="outline"><Link to="/vendors">Open Vendors</Link></Button>}>Add vendors on the Vendors page, then record each one's risk tier and documents on its Risk tab.</EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Vendor risk table">
            <Table>
              <TableCaption className="sr-only">Vendor risk, the riskiest first{f.filtered ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Vendor</TableHead><TableHead scope="col">Risk tier</TableHead><TableHead scope="col">Assurance report</TableHead>
                <TableHead scope="col">Documents in force</TableHead><TableHead scope="col">DPA</TableHead><TableHead scope="col">Next expiry</TableHead>
                <TableHead scope="col">Gaps</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((r) => <OverviewRow key={r.vendor.id} r={r} />)}
              </TableBody>
            </Table>
          </TableRegion>
          <p className="mt-2 text-sm text-muted-foreground">{rows.length} {rows.length === 1 ? "vendor" : "vendors"}. Sorted by risk tier (critical first, not assessed last), then name. Replaced documents aren't counted.</p>
        </>
      )}
    </>
  );
}

function OverviewRow({ r }: { r: RiskOverviewRow }) {
  const inForce = r.document_list.filter((d) => !d.superseded);
  return (
    <TableRow className="align-top" data-vendor={r.vendor.name}>
      <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
        <Link to={riskPath(r.vendor.id)} className="underline">{r.vendor.name}</Link>
        {!r.has_profile && <span className="block text-sm font-normal text-muted-foreground">No risk file yet</span>}
      </TableHead>
      <TableCell><TierMark tier={r.risk_tier} /></TableCell>
      <TableCell className="whitespace-normal">
        {r.assurance.type && <span className="block">{assuranceTypeLabel(r.assurance.type)}</span>}
        <AssuranceStatusChip status={r.assurance.status} />
      </TableCell>
      <TableCell className="min-w-56 whitespace-normal">
        {inForce.length === 0 ? <span className="text-muted-foreground">None</span> : (
          <ul className="grid gap-1">
            {inForce.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center gap-1.5">
                <DocumentStatusChip status={d.status} />
                <span>{kindLabel(d.kind)}{d.title ? ` (${d.title})` : ""}</span>
              </li>
            ))}
          </ul>
        )}
      </TableCell>
      <TableCell className="whitespace-normal">
        {r.dpa_status ? dpaLabel(r.dpa_status) : <span className="text-muted-foreground">Not recorded</span>}
        {r.processes_personal_data && <span className="block text-sm text-muted-foreground">Processes personal data</span>}
      </TableCell>
      <TableCell className="tabular">{r.next_expiry ?? <span className="text-muted-foreground">None</span>}</TableCell>
      <TableCell className="whitespace-normal">{r.gaps.length ? <RiskGaps gaps={r.gaps} /> : <span className="text-success-foreground"><span aria-hidden="true">✓ </span>No gaps</span>}</TableCell>
    </TableRow>
  );
}

// ---- the documents register ----------------------------------------------------------------------------------------------

const DOC_FILTERS = ["vendor", "kind", "status", "tier", "expiring_within", "superseded"] as const;
type DocQuery = QueryOf<"vendor-risk-documents", "get">;
const STATUS_CHOICES = [{ value: "current", label: "Current" }, { value: "expiring", label: "Expiring" }, { value: "expired", label: "Expired" }];
/** The URL's `superseded`: absent hides replaced documents (the API's `false`); `all` shows every one; `true` only them. */
const REPLACED_CHOICES = [{ value: "", label: "Hide replaced documents" }, { value: "all", label: "Show replaced documents too" }, { value: "true", label: "Only replaced documents" }];

export function VendorRiskDocumentsPage() {
  const me = useMe();
  if (!me.capabilities.includes("vendor_risk")) return <><PageHead title="Vendor risk documents" /><VendorRiskUnavailable /></>;
  return <Register />;
}

function Register() {
  const f = useUrlFilters(DOC_FILTERS);
  const ordering = f.params.get("ordering") === "-expiry" ? "-expiry" : "";
  const cursor = f.params.get("cursor") ?? "";
  const query: Record<string, string> = {};
  for (const k of DOC_FILTERS) if (f.value(k) && k !== "superseded") query[k] = f.value(k);
  const replaced = f.value("superseded");
  if (replaced === "true") query.superseded = "true";
  else if (replaced !== "all") query.superseded = "false";
  if (ordering) query.ordering = ordering;
  const exportSearch = new URLSearchParams(Object.fromEntries(Object.entries(query).filter(([k]) => k !== "ordering"))).toString();
  const q = useTypedQuery("vendor-risk-documents", { query: { ...query, ...(cursor ? { cursor } : {}) } as Partial<DocQuery> });
  const vendors = useApiQuery<{ id: string; name: string }[]>("vendors");
  const [notice, setNotice] = useState("");
  const rows = q.data?.results;
  const vendorOptions = (vendors.data ?? []).map((v) => ({ value: v.id, label: v.name }));
  if (f.value("vendor") && !vendorOptions.some((o) => o.value === f.value("vendor"))) {
    vendorOptions.push({ value: f.value("vendor"), label: vendors.isError ? "Vendor (names didn't load)" : vendors.data ? "Deleted vendor" : "Loading vendor…" });
  }
  const descending = ordering === "-expiry";
  return (
    <>
      <PageHead title="Vendor risk documents" desc="Every vendor's dated risk documents, the soonest expiry first. A replaced document has a later one of the same kind.">
        <ExportCsvButton path={`vendor-risk-documents/export.csv?${exportSearch}`} fallback="softwarden-vendor-risk-documents.csv" what="the risk documents" onDone={setNotice} />
      </PageHead>
      <VendorRiskSections />
      <SuccessNote text={notice} />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the risk documents" onSubmit={(e) => e.preventDefault()}>
        <SelectFilter id="docs-vendor" label="Vendor" value={f.value("vendor")} onChange={(v) => f.set("vendor", v)} options={vendorOptions} all="Every vendor" />
        <SelectFilter id="docs-kind" label="Kind" value={f.value("kind")} onChange={(v) => f.set("kind", v)} options={DOCUMENT_KINDS} />
        <SelectFilter id="docs-status" label="Status" value={f.value("status")} onChange={(v) => f.set("status", v)} options={STATUS_CHOICES} />
        <SelectFilter id="docs-tier" label="Vendor's risk tier" value={f.value("tier")} onChange={(v) => f.set("tier", v)} options={TIER_CHOICES} />
        <CommitInput id="docs-expiring" label="Expiring within (days)" value={f.value("expiring_within")} onChange={(v) => f.set("expiring_within", v, true)} numeric className="w-36" placeholder="Days" />
        <SelectFilter id="docs-replaced" label="Replaced documents" value={replaced === "all" || replaced === "true" ? replaced : ""} onChange={(v) => f.set("superseded", v)}
          options={REPLACED_CHOICES} all={null} />
        {(f.filtered || ordering) && <Button type="button" variant="outline" onClick={f.clear}>Clear filters</Button>}
      </form>
      {vendors.isError && (
        <p className="-mt-2 mb-4 flex flex-wrap items-center gap-2 text-sm text-error-foreground">
          <span><span aria-hidden="true">! </span>Couldn't load the vendor names for the Vendor filter.</span>
          <Button size="sm" variant="outline" onClick={() => void vendors.refetch()}>Retry<span className="sr-only"> loading the vendor names</span></Button>
        </p>
      )}
      <p className="sr-only" aria-live="polite">{rows ? `${rows.length} ${rows.length === 1 ? "document" : "documents"} on this page.` : ""}</p>
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={filterProblem(q.error, "Couldn't load the risk documents.")} onRetry={() => void q.refetch()} />
          {f.filtered && <Button variant="outline" onClick={f.clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the risk documents" rows={5} />}
      {rows && rows.length === 0 && (f.filtered
        ? <EmptyState title="No documents match these filters" action={<Button variant="outline" onClick={f.clear}>Clear filters</Button>}>Try another status or kind, or show replaced documents too.</EmptyState>
        : <EmptyState title="No risk documents yet" action={<Button asChild variant="outline"><Link to="/vendor-risk">Open the vendor risk overview</Link></Button>}>Add SOC 2 reports, ISO 27001 certificates, DPAs and other dated documents on a vendor's Risk tab.</EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Risk documents table">
            <Table>
              <TableCaption className="sr-only">Vendor risk documents, by expiry {descending ? "latest first" : "soonest first"}{f.filtered ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Document</TableHead><TableHead scope="col">Vendor</TableHead><TableHead scope="col">Issued</TableHead>
                <TableHead scope="col" aria-sort={descending ? "descending" : "ascending"}>
                  <button type="button" className="inline-flex min-h-6 items-center gap-1 font-semibold" onClick={() => f.set("ordering", descending ? "" : "-expiry")}>
                    Expires<span aria-hidden="true">{descending ? "▼" : "▲"}</span><span className="sr-only">, sort {descending ? "soonest first" : "latest first"}</span>
                  </button>
                </TableHead>
                <TableHead scope="col">Status</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((d) => (
                  <TableRow key={d.id} className={d.superseded ? "align-top text-muted-foreground" : "align-top"} data-document={d.id} data-superseded={d.superseded ? "" : undefined}>
                    <TableHead scope="row" className={d.superseded ? "max-w-xs whitespace-normal font-normal text-muted-foreground" : "max-w-xs whitespace-normal font-semibold text-foreground"}>
                      {kindLabel(d.kind)}{d.title && <span className="block font-normal">{d.title}</span>}
                    </TableHead>
                    <TableCell className="whitespace-normal"><Link to={riskPath(d.vendor.id)} className="underline">{d.vendor.name}</Link></TableCell>
                    <TableCell className="tabular">{d.issued_on ?? "—"}</TableCell>
                    <TableCell className="whitespace-normal">
                      {d.expires_on ? <span className="tabular">{d.expires_on}</span> : <span className="text-muted-foreground">No expiry</span>}
                      {d.expires_on && <span className="block text-sm text-muted-foreground">{daysText(d.days_left)}</span>}
                    </TableCell>
                    <TableCell className="whitespace-normal"><span className="inline-flex flex-wrap gap-1.5"><DocumentStatusChip status={d.status} />{d.superseded && <ReplacedChip />}</span></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <nav aria-label="Pages" className="mt-3 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span className="mr-auto">{rows.length} {rows.length === 1 ? "document" : "documents"} on this page, by expiry {descending ? "latest first, undated ones first" : "soonest first, undated ones last"}.</span>
            <Button size="sm" variant="outline" disabled={!q.data?.previous} onClick={() => f.setCursor(q.data?.previous ?? null)}>Previous</Button>
            <Button size="sm" variant="outline" disabled={!q.data?.next} onClick={() => f.setCursor(q.data?.next ?? null)}>Next</Button>
          </nav>
        </>
      )}
    </>
  );
}
