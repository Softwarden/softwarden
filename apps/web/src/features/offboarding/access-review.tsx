/** Access review (`/access-review`, every role reads; loaded on first use): every record that isn't
 *  archived with its business and technical owner, the Entra owners of its linked discovered apps, and flags as chips:
 *  the gaps ⚠ Inactive owner, ⚠ Owner left, ⚠ No owner, and the informational ⓘ Entra owners differ (GET
 *  reports/access-review). Filters (kind, flag, owner, include_retired) live in the URL; the counts are over the records
 *  in scope before the flag filter, as the API gives them. Export CSV is hidden while platform staff impersonate (the API
 *  refuses it). Owners link to their person page, where their roles can be reassigned. */
import { useState } from "react";
import { Link } from "react-router-dom";
import type { Person } from "@/api";
import { EmptyState, ErrorBanner, Loading, PageHead, TableRegion } from "@/components/app";
import { Combobox } from "@/components/combobox";
import { chip, ChipGroup, GapChip, LifecycleChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ExportCsvButton, filterProblem, SelectFilter, useUrlFilters } from "@/components/url-filters";
import { KIND_LABEL, isCatalogKind } from "@/lib/kinds";
import { useTypedQuery, type QueryOf, type Schemas } from "@/lib/openapi";
import { useApiQuery } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";

type Row = Schemas["AccessReviewRow"];
type Owner = Schemas["AccessReviewOwner"];
type Flag = Schemas["FlagsEnum"];
type Query = QueryOf<"reports/access-review", "get">;

const FILTERS = ["kind", "flag", "owner", "include_retired"] as const;
const KINDS = [{ value: "application", label: "Applications" }, { value: "component", label: "Components" }, { value: "ai_agent", label: "AI agents" }, { value: "integration", label: "Integrations" }];
const FLAG_LABEL: Record<Flag, string> = { inactive_owner: "Inactive owner", owner_left: "Owner left", no_owner: "No owner", entra_owner_mismatch: "Entra owners differ" };
const FLAGS = (Object.keys(FLAG_LABEL) as Flag[]).map((f) => ({ value: f, label: FLAG_LABEL[f] }));

function FlagChip({ flag }: { flag: Flag }) {
  if (flag === "entra_owner_mismatch") return <span className={chip({ state: "neutral" })} data-flag={flag}><span aria-hidden="true">ⓘ</span>{FLAG_LABEL[flag]}</span>;
  return <span data-flag={flag}><GapChip label={FLAG_LABEL[flag]} /></span>;
}

function OwnerCell({ owner, gap }: { owner: Owner | null; gap: string | null }) {
  if (!owner) return gap ? <GapChip label={gap} /> : <span className="text-muted-foreground">—</span>;
  return (
    <>
      <Link to={`/people/${owner.id}`} className="underline">{owner.name}</Link>
      {owner.status !== "active" && <span className="block text-sm text-muted-foreground">{owner.status === "inactive" ? "Marked inactive" : "Account deactivated or removed"}</span>}
    </>
  );
}

export function AccessReviewPage() {
  const me = useMe();
  const f = useUrlFilters(FILTERS);
  const query: Record<string, string> = {};
  for (const k of FILTERS) if (f.value(k)) query[k] = f.value(k);
  const search = new URLSearchParams(query).toString();
  const q = useTypedQuery("reports/access-review", { query: query as Partial<Query> });
  const people = useApiQuery<Person[]>("people");
  const [notice, setNotice] = useState("");
  const rows = q.data?.results;
  const counts = q.data?.counts;
  const owners = (people.data ?? []).map((p) => ({ id: p.id, label: p.active ? p.name : `${p.name} (inactive)`, hint: p.email }));
  const ownerId = f.value("owner");
  if (ownerId && !owners.some((o) => o.id === ownerId)) owners.push({ id: ownerId, label: people.data ? "Unknown person" : "Loading…", hint: "" });
  return (
    <>
      <PageHead title="Access review" desc="Every record with its owners, flagged where an owner is inactive or has left, where there is no owner, and where Entra lists other owners.">
        <ExportCsvButton path={`reports/access-review/export.csv${search ? `?${search}` : ""}`} fallback="softwarden-access-review.csv" what="the access review" onDone={setNotice} />
      </PageHead>
      <SuccessNote text={notice} />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the access review" onSubmit={(e) => e.preventDefault()}>
        <SelectFilter id="ar-kind" label="Kind" value={f.value("kind")} onChange={(v) => f.set("kind", v)} options={KINDS} all="Every kind" className="w-44" />
        <SelectFilter id="ar-flag" label="Flag" value={f.value("flag")} onChange={(v) => f.set("flag", v)} options={FLAGS} all="Any or none" />
        <div className="flex w-64 flex-col gap-1.5">
          <Label htmlFor="ar-owner">Owner</Label>
          <Combobox id="ar-owner" options={owners} value={ownerId} onChange={(v) => f.set("owner", v)} placeholder="Search people…" clearLabel="Anyone" listLabel="Owner suggestions" />
        </div>
        <label className="flex min-h-9 items-center gap-2 text-sm">
          <input type="checkbox" className="h-4 w-4 accent-primary" checked={f.value("include_retired") === "true"} onChange={(e) => f.set("include_retired", e.target.checked ? "true" : "")} />
          Include retired records
        </label>
        {f.filtered && <Button type="button" variant="outline" onClick={f.clear}>Clear filters</Button>}
      </form>
      {counts && (
        <p className="mb-3" data-testid="access-counts" role="status">
          {counts.total} {counts.total === 1 ? "record" : "records"} in scope, {counts.flagged} flagged: {counts.inactive_owner} with an inactive owner, {counts.owner_left} whose owner left, {counts.no_owner} with no owner, {counts.entra_owner_mismatch} where Entra lists other owners.
        </p>
      )}
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={filterProblem(q.error, "Couldn't load the access review.")} onRetry={() => void q.refetch()} />
          {f.filtered && <Button variant="outline" onClick={f.clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the access review" rows={5} />}
      {rows && rows.length === 0 && (f.filtered
        ? <EmptyState title="No records match these filters" action={<Button variant="outline" onClick={f.clear}>Clear filters</Button>}>Try another kind or flag, or anyone as owner.</EmptyState>
        : <EmptyState title="No records yet" action={canWrite(me) && <Button asChild variant="outline"><Link to="/applications">Open Applications</Link></Button>}>
            The access review lists applications, components, AI agents and integrations with their owners. {canWrite(me) ? "Add or import records first." : "Editors and Admins add them."}
          </EmptyState>)}
      {rows && rows.length > 0 && (
        <TableRegion label="Access review table">
          <Table>
            <TableCaption className="sr-only">Access review{f.filtered ? ", filtered" : ""}</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Record</TableHead><TableHead scope="col">Lifecycle</TableHead><TableHead scope="col">Business owner</TableHead>
              <TableHead scope="col">Technical owner</TableHead><TableHead scope="col">Flags</TableHead><TableHead scope="col">Entra owners</TableHead><TableHead scope="col">Why</TableHead>
            </TableRow></TableHeader>
            <TableBody>{rows.map((r) => <ReviewRow key={r.record.id} r={r} />)}</TableBody>
          </Table>
        </TableRegion>
      )}
    </>
  );
}

function ReviewRow({ r }: { r: Row }) {
  const integration = r.record.kind === "integration";
  return (
    <TableRow className="align-top" data-record={r.record.name}>
      <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
        {r.record.path ? <Link to={r.record.path} className="underline">{r.record.name}</Link> : r.record.name}
        <span className="block text-sm font-normal text-muted-foreground">{isCatalogKind(r.record.kind) ? KIND_LABEL[r.record.kind] : r.record.kind}</span>
      </TableHead>
      <TableCell><LifecycleChip value={r.record.lifecycle} /></TableCell>
      <TableCell className="whitespace-normal"><OwnerCell owner={r.business_owner} gap={integration ? null : "No business owner"} /></TableCell>
      <TableCell className="whitespace-normal"><OwnerCell owner={r.technical_owner} gap="No technical owner" /></TableCell>
      <TableCell className="whitespace-normal">
        {r.flags.length ? <ChipGroup>{r.flags.map((x) => <FlagChip key={x} flag={x} />)}</ChipGroup>
          : <span className="text-success-foreground"><span aria-hidden="true">✓ </span>No flags</span>}
      </TableCell>
      <TableCell className="min-w-40 whitespace-normal">
        {r.entra_owners.length === 0 ? <span className="text-muted-foreground">None linked</span> : (
          <ul className="grid gap-0.5">
            {r.entra_owners.map((o, i) => (
              <li key={`${o.email}-${i}`}>
                {o.person ? <Link to={`/people/${o.person.id}`} className="underline">{o.person.name}</Link> : (o.name || o.email || <span className="italic">Owner not resolved</span>)}
                <span className="block text-sm text-muted-foreground">From {o.source}</span>
              </li>
            ))}
          </ul>
        )}
      </TableCell>
      <TableCell className="min-w-48 whitespace-normal text-sm">{r.why || "—"}</TableCell>
    </TableRow>
  );
}
