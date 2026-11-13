import { useState } from "react";
import { Link } from "react-router-dom";
import { api, CatalogRecord, cap, Integration, LinkEnd, Page, Relationship, RelationshipTypeBrief } from "@/api";
import { attempt, Chip, ErrorBanner, ErrorSummary, Field, Loading, Select, TableRegion, useLoad, useReturnFocus } from "@/components/app";
import { ConfirmLinkDialog, ConfirmTarget, HistoryDialog } from "@/features/catalog/link-confirmations";
import { InstancePicker, PickedRecord, RecordPicker, useInstanceGate } from "@/components/record-picker";
import { LifecycleChip, LinkStateChip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { KIND_LABEL, type RecordKind } from "@/lib/kinds";
import { problemText } from "@/lib/query";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { inverseOf, phraseOf, sortTypes, useRelationshipTypes } from "@/lib/relationship-types";
import { RecordLink } from "@/components/record-link";

/** Relationships tab on application, component and AI agent detail (type labels from the API).
 *  A link is read from this record's side: as the source it reads as its type's name ("Depends on"), as the
 *  target as the inverse name ("Is needed by"). Dependency types get a section per direction, other types one section
 *  with a Relationship column, and components an Implements section. The core sections are always shown, with their
 *  empty text; an added type's sections appear once a link uses it. */

type Kind = RecordKind;
/** Which end of a link this record is: "out" for the source (the type's name), "in" for the target (its inverse). */
type Dir = "out" | "in";
const dirOf = (rel: Relationship, id: string): Dir => (rel.source.id === id ? "out" : "in");
/** A link's type, from the link itself; an older API without `relationship_type` gets the slug in words. */
const typeOfLink = (r: Relationship): RelationshipTypeBrief =>
  r.relationship_type ?? { id: "", slug: r.type, name: cap(r.type.replace(/_/g, " ")), inverse_name: "", is_dependency: false, is_core: false };
/** How a link reads from one side, as a row or section label ("Depends on", "Is needed by"). */
const readingOf = (t: Pick<RelationshipTypeBrief, "name" | "inverse_name">, dir: Dir) => (dir === "out" ? t.name : inverseOf(t));

const svg = (children: React.ReactNode) => <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
const IconLink = svg(<><path d="M6.5 9.5a3 3 0 0 0 4.2 0l2-2a3 3 0 0 0-4.2-4.2l-.8.8" /><path d="M9.5 6.5a3 3 0 0 0-4.2 0l-2 2a3 3 0 0 0 4.2 4.2l.8-.8" /></>);

/** The core types' seeded labels, used only for an empty core section while the type list hasn't loaded (or failed);
 *  the API's labels, which an Admin may rename, replace them as soon as the list or a link of the type arrives. */
const CORE_FALLBACK: Record<string, RelationshipTypeBrief> = {
  depends_on: { id: "", slug: "depends_on", name: "Depends on", inverse_name: "Is needed by", is_dependency: true, is_core: true },
  replaces: { id: "", slug: "replaces", name: "Replaces", inverse_name: "Is replaced by", is_dependency: false, is_core: true },
  implements: { id: "", slug: "implements", name: "Implements", inverse_name: "Is implemented by", is_dependency: false, is_core: true },
};

type Section = { id: string; title: string; type: RelationshipTypeBrief; dirs: Dir[]; empty?: string };

/** The sections for a record, in order: depends_on each way, added dependency types each way, replaces, other added
 *  types, implements (components only). */
function sectionsFor(rec: CatalogRecord, kind: Kind, rows: Relationship[], listed: RelationshipTypeBrief[] | undefined): Section[] {
  const bySlug = new Map<string, RelationshipTypeBrief>(Object.entries(CORE_FALLBACK));
  for (const r of rows) bySlug.set(r.type, typeOfLink(r));
  for (const t of listed ?? []) bySlug.set(t.slug, t);
  const has = (slug: string, dir?: Dir) => rows.some((r) => r.type === slug && (!dir || dirOf(r, rec.id) === dir));
  const name = rec.name;
  const dep = bySlug.get("depends_on")!;
  const rep = bySlug.get("replaces")!;
  const added = sortTypes([...bySlug.values()].filter((t) => !(t.slug in CORE_FALLBACK)));
  const sections: Section[] = [
    { id: "depends_on", title: dep.name, type: dep, dirs: ["out"], empty: `${name} has no recorded dependencies.` },
    { id: "used_by", title: inverseOf(dep), type: dep, dirs: ["in"], empty: `Nothing is recorded as depending on ${name}. That doesn't mean nothing does.` },
  ];
  for (const t of added.filter((x) => x.is_dependency)) {
    if (has(t.slug, "out")) sections.push({ id: `${t.slug}-out`, title: t.name, type: t, dirs: ["out"] });
    if (has(t.slug, "in")) sections.push({ id: `${t.slug}-in`, title: inverseOf(t), type: t, dirs: ["in"] });
  }
  sections.push({ id: "replaces", title: `${rep.name} / ${phraseOf(inverseOf(rep))}`, type: rep, dirs: ["out", "in"], empty: `${name} doesn't replace, and isn't replaced by, another record.` });
  for (const t of added.filter((x) => !x.is_dependency && has(x.slug))) sections.push({ id: t.slug, title: `${t.name} / ${phraseOf(inverseOf(t))}`, type: t, dirs: ["out", "in"] });
  if (kind === "component") {
    const impl = bySlug.get("implements")!;
    sections.push({ id: "implements", title: impl.name, type: impl, dirs: ["out"], empty: `${name} doesn't implement any integration.` });
  }
  return sections;
}

export function RelationshipsTab({ rec, kind, rels, write }: { rec: CatalogRecord; kind: Kind; rels: ReturnType<typeof useLoad<Relationship[]>>; write: boolean }) {
  const [editing, setEditing] = useState<Relationship | "new" | null>(null);
  const [removing, setRemoving] = useState<Relationship | null>(null);
  const [confirming, setConfirming] = useState<ConfirmTarget | null>(null);
  const [history, setHistory] = useState<ConfirmTarget | null>(null);
  const [notice, setNotice] = useState("");
  const types = useRelationshipTypes();
  const editFocus = useReturnFocus(editing !== null);
  const removeFocus = { onCloseAutoFocus: (e: Event) => { e.preventDefault(); document.getElementById("h-rel-add")?.focus(); } };
  const rows = rels.data;
  function done(text: string) { setEditing(null); setRemoving(null); setConfirming(null); setNotice(text); rels.reload(); }
  if (rels.error) return <ErrorBanner message={rels.error} onRetry={rels.reload} />;
  if (!rows) return <Loading what="relationships" rows={3} />;
  const sections = sectionsFor(rec, kind, rows, types.data);
  return (
    <>
      <SuccessNote text={notice} />
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-sm text-muted-foreground">Links are recorded by hand and say what is known, not everything that exists. Link state shows when each was last confirmed.</p>
        {write && <Button id="h-rel-add" variant="outline" onClick={() => setEditing("new")}>Add relationship</Button>}
      </div>
      <div className="grid gap-4">
        {sections.map((s) => {
          const mine = rows.filter((r) => r.type === s.type.slug && s.dirs.includes(dirOf(r, rec.id)));
          return (
            <Panel key={s.id} id={`h-rel-${s.id}`} icon={IconLink} title={s.title} count={mine.length}>
              {mine.length === 0 ? <p className="px-4 py-3 text-muted-foreground">{s.empty}</p> : (
                <LinkTable rec={rec} title={s.title} rows={mine} withRole={s.dirs.length > 1} write={write} onEdit={setEditing} onRemove={setRemoving} onConfirm={(r) => setConfirming(confirmTarget(r))} onHistory={(r) => setHistory(confirmTarget(r))} />
              )}
            </Panel>
          );
        })}
        <DataFlows rec={rec} write={write} />
      </div>

      <ConfirmLinkDialog target={confirming} onClose={() => setConfirming(null)} onDone={done} />
      <HistoryDialog target={history} onClose={() => setHistory(null)} />
      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="max-w-lg" {...editFocus}>
          <DialogHeader>
            <DialogTitle>{editing === "new" || editing === null ? `Add relationship — ${rec.name}` : `Edit relationship — ${rec.name}`}</DialogTitle>
            <DialogDescription>Record how {rec.name} is connected to another record. A new or changed link needs confirming again.</DialogDescription>
          </DialogHeader>
          {editing === "new" && <RelationshipForm rec={rec} kind={kind} rel={null} onCancel={() => setEditing(null)} onDone={done} />}
          {editing && editing !== "new" && (
            <OpenedEdit<Relationship> path={`relationships/${editing.id}`} what="relationship">
              {(rel, write) => <RelationshipForm rec={rec} kind={kind} rel={rel} write={write} onCancel={() => setEditing(null)} onDone={done} />}
            </OpenedEdit>
          )}
        </DialogContent>
      </Dialog>
      <Dialog open={removing !== null} onOpenChange={(o) => !o && setRemoving(null)}>
        <DialogContent className="max-w-md" {...removeFocus}>
          {removing && <RemoveLink rel={removing} onCancel={() => setRemoving(null)} onDone={done} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

export const confirmTarget = (r: Relationship): ConfirmTarget => ({ kind: "relationship", id: r.id, sentence: relationshipSentence(r), state: r.link_state, why: r.link_why });

function endText(e: LinkEnd) { return e.instance ? `${e.name} (${e.instance.name})` : e.name; }
/** The link in words, from the source: "Portal depends on Sync". */
export const relationshipSentence = (r: Relationship) => `${endText(r.source)} ${phraseOf(r.relationship_type?.name, r.type)} ${endText(r.target)}`;

function LinkTable({ rec, title, rows, withRole, write, onEdit, onRemove, onConfirm, onHistory }: {
  rec: CatalogRecord; title: string; rows: Relationship[]; withRole: boolean; write: boolean;
  onEdit: (r: Relationship) => void; onRemove: (r: Relationship) => void; onConfirm: (r: Relationship) => void; onHistory: (r: Relationship) => void;
}) {
  const view = rows.map((r) => {
    const dir = dirOf(r, rec.id);
    return { r, dir, other: dir === "in" ? r.source : r.target };
  }).sort((a, b) => a.other.name.localeCompare(b.other.name));
  return (
    <TableRegion label={`${title} table`}>
      <Table>
        <TableCaption className="sr-only">{title} for {rec.name}, sorted by record name A to Z</TableCaption>
        <TableHeader><TableRow>
          <TableHead aria-sort="ascending">Record</TableHead>
          {withRole && <TableHead>Relationship</TableHead>}
          <TableHead>Link state</TableHead><TableHead>Purpose</TableHead>
          <TableHead><span className="sr-only">Actions</span></TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {view.map(({ r, dir, other }) => (
            <TableRow key={r.id} className="h-[52px] align-top">
              <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                <RecordLink record={other} />
                {other.kind !== "application" && <span className="ml-1 font-normal text-muted-foreground">{KIND_LABEL[other.kind] ?? cap(other.kind)}</span>}
                {other.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
                {(r.source.instance || r.target.instance) && (
                  <span className="block text-[13px] font-normal text-muted-foreground">
                    <span className="sr-only">Instances: </span>{r.source.instance?.name ?? "any instance"} <span aria-hidden="true">→</span><span className="sr-only"> to </span> {r.target.instance?.name ?? "any instance"}
                  </span>
                )}
              </TableHead>
              {withRole && <TableCell>{readingOf(typeOfLink(r), dir)}</TableCell>}
              <TableCell className="whitespace-normal">
                <LinkStateChip state={r.link_state} />
                <span className="mt-1 block max-w-56 text-[13px] text-muted-foreground">{r.link_why}</span>
              </TableCell>
              <TableCell className="max-w-sm whitespace-normal text-muted-foreground [overflow-wrap:anywhere]">{r.purpose || "—"}</TableCell>
              <TableCell className="whitespace-normal text-right">
                <span className="inline-flex flex-wrap justify-end gap-2">
                  {write && <Button size="sm" variant="outline" onClick={() => onConfirm(r)}>Confirm link<span className="sr-only"> with {other.name}</span></Button>}
                  <Button size="sm" variant="outline" onClick={() => onHistory(r)}>History<span className="sr-only"> of confirmations for link with {other.name}</span></Button>
                  {write && <Button size="sm" variant="outline" onClick={() => onEdit(r)}>Edit<span className="sr-only"> link with {other.name}</span></Button>}
                  {write && <Button size="sm" variant="outline" onClick={() => onRemove(r)}>Remove<span className="sr-only"> link with {other.name}</span></Button>}
                </span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

// ---- Add / edit dialog -----------------------------------------------------------------------------------

const toPicked = (e: LinkEnd): PickedRecord => ({ id: e.id, name: e.name, kind: e.kind });
/** A choice in the Relationship select: a type and which end this record is ("depends_on|out"). */
const choiceOf = (slug: string, dir: Dir) => `${slug}|${dir}`;
const parseChoice = (v: string): { slug: string; dir: Dir } => {
  const at = v.lastIndexOf("|");
  return { slug: v.slice(0, at), dir: v.slice(at + 1) === "in" ? "in" : "out" };
};

/** The type list comes from the API, so a type an Admin added is offered and an edited link keeps its own type. */
function RelationshipForm({ rec, kind, rel, write, onCancel, onDone }: {
  rec: CatalogRecord; kind: Kind; rel: Relationship | null; write?: OpenedWrite; onCancel: () => void; onDone: (text: string) => void;
}) {
  const types = useRelationshipTypes();
  if (types.isError && !rel) {
    return (
      <>
        <ErrorBanner message={`${problemText(types.error, "Couldn't load the relationship types.")} Nothing was saved.`} onRetry={() => void types.refetch()} />
        <DialogFooter><Button type="button" variant="outline" onClick={onCancel}>Cancel</Button></DialogFooter>
      </>
    );
  }
  if (types.isPending) return <Loading what="relationship types" rows={2} />;
  // An edit keeps the link's own type even when the list couldn't load or doesn't have it.
  const listed = types.data ?? [];
  const all = rel && !listed.some((t) => t.slug === rel.type) ? [...listed, typeOfLink(rel)] : listed;
  return <RelationshipFormBody rec={rec} kind={kind} rel={rel} write={write} types={sortTypes(all)} onCancel={onCancel} onDone={onDone} />;
}

function RelationshipFormBody({ rec, kind, rel, write, types, onCancel, onDone }: {
  rec: CatalogRecord; kind: Kind; rel: Relationship | null; write?: OpenedWrite; types: (RelationshipTypeBrief & { description?: string })[]; onCancel: () => void; onDone: (text: string) => void;
}) {
  const initialDir: Dir = rel ? dirOf(rel, rec.id) : "out";
  const [choice, setChoice] = useState(rel ? choiceOf(rel.type, initialDir) : choiceOf("depends_on", "out"));
  const { slug, dir } = parseChoice(choice);
  const otherEnd = rel ? (initialDir === "in" ? rel.source : rel.target) : null;
  const thisEnd = rel ? (initialDir === "in" ? rel.target : rel.source) : null;
  const [other, setOther] = useState<PickedRecord | null>(otherEnd ? toPicked(otherEnd) : null);
  const [thisInstance, setThisInstance] = useState(thisEnd?.instance?.id ?? "");
  const [otherInstance, setOtherInstance] = useState(otherEnd?.instance?.id ?? "");
  const [purpose, setPurpose] = useState(rel?.purpose ?? "");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const gate = useInstanceGate();

  // The API's endpoint rules: implements goes from a component to an integration; replaces joins two records of the
  // same kind; depends_on and every added type join applications, components and AI agents.
  const otherKinds = (s: string): PickedRecord["kind"][] => (s === "implements" ? ["integration"] : s === "replaces" ? [kind] : ["application", "component", "ai_agent"]);
  const offered = types.filter((t) => t.slug !== "implements" || kind === "component" || rel?.type === "implements");
  const optionsOf = (t: RelationshipTypeBrief) => (t.slug === "implements"
    ? [{ value: choiceOf(t.slug, "out"), label: `${rec.name} ${phraseOf(t.name, t.slug)}… (an integration)` }]
    : [
        { value: choiceOf(t.slug, "out"), label: `${rec.name} ${phraseOf(t.name, t.slug)}…` },
        { value: choiceOf(t.slug, "in"), label: `${rec.name} ${phraseOf(inverseOf(t))}… (the other record ${phraseOf(t.name, t.slug)} it)` },
      ]);
  const groups = [
    { label: "Dependencies", types: offered.filter((t) => t.is_dependency) },
    { label: "Other relationships", types: offered.filter((t) => !t.is_dependency) },
  ].filter((g) => g.types.length);
  const chosen = types.find((t) => t.slug === slug);
  function changeChoice(next: string) {
    setChoice(next);
    if (other && !otherKinds(parseChoice(next).slug).includes(other.kind)) { setOther(null); setOtherInstance(""); }
  }
  // The API names the ends source and target; the form names them this record and the other record.
  const fwd = dir === "out";
  const fieldOf: Record<string, string> = {
    type: "rel-role", source: fwd ? "rel-role" : "rel-other", target: fwd ? "rel-other" : "rel-role",
    source_instance: fwd ? "rel-this-instance" : "rel-other-instance", target_instance: fwd ? "rel-other-instance" : "rel-this-instance",
    purpose: "rel-purpose",
  };
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!other) return setErrors({ "rel-other": ["Choose a record."] });
    const unsettled = gate.errors();
    if (Object.keys(unsettled).length) return setErrors(unsettled);
    const mine = thisInstance || null, theirs = otherInstance || null;
    const body = {
      type: slug, source: fwd ? rec.id : other.id, target: fwd ? other.id : rec.id,
      source_instance: fwd ? mine : theirs, target_instance: fwd ? theirs : mine, purpose: purpose.trim(),
    };
    setBusy(true);
    const failed = await attempt(async () => api(rel ? `relationships/${rel.id}` : "relationships", { method: rel ? "PATCH" : "POST", body, headers: await write?.ifMatch() }));
    setBusy(false);
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) {
      const mapped: Record<string, string[]> = {};
      for (const [k, v] of Object.entries(failed.errors)) mapped[fieldOf[k] ?? "rel-other"] = [...(mapped[fieldOf[k] ?? "rel-other"] ?? []), ...v];
      return setErrors(Object.keys(mapped).length ? mapped : { "rel-other": [failed.detail] });
    }
    onDone(rel ? `Relationship with ${other.name} saved.` : `Relationship with ${other.name} added.`);
  }
  const help = slug === "implements" ? "Search integrations by name." : slug === "replaces" ? "Search records of the same kind by name." : "Search applications, components and AI agents by name.";
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="rel-role" label="Relationship (required)" errors={errors} help={chosen?.description || undefined}>
        <Select id="rel-role" value={choice} onChange={(e) => changeChoice(e.target.value)}>
          {groups.map((g) => (
            <optgroup key={g.label} label={g.label}>
              {g.types.flatMap(optionsOf).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </optgroup>
          ))}
        </Select>
      </Field>
      <Field id="rel-other" label="Other record (required)" errors={errors} help={help}>
        <RecordPicker id="rel-other" value={other} kinds={otherKinds(slug)} listLabel="Record suggestions"
          onChange={(r) => { setOther(r); setOtherInstance(""); }} />
      </Field>
      {kind === "application" && <InstancePicker id="rel-this-instance" label={`Instance of ${rec.name} (optional)`} record={{ id: rec.id, name: rec.name, kind }} value={thisInstance} onChange={setThisInstance} errors={errors} onBlocked={gate.report} />}
      <InstancePicker id="rel-other-instance" label={`Instance of ${other?.name ?? "the other record"} (optional)`} record={other} value={otherInstance} onChange={setOtherInstance} errors={errors} onBlocked={gate.report} />
      <Field id="rel-purpose" label="Purpose" errors={errors} help="Why the link exists, for example “Payroll export runs nightly”.">
        <textarea id="rel-purpose" rows={2} value={purpose} onChange={(e) => setPurpose(e.target.value)}
          className="w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-2 text-base shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive md:text-sm" />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : rel ? "Save" : "Add relationship"}</Button>
      </DialogFooter>
    </form>
  );
}

export function RemoveLink({ rel, onCancel, onDone }: { rel: Relationship; onCancel: () => void; onDone: (t: string) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Read with its ETag as the confirmation opens; the removal sends it as If-Match.
  const opened = useOpenedRecord(`relationships/${rel.id}`);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const failed = await attempt(async () => api(`relationships/${rel.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) }));
    setBusy(false);
    if (failed?.status === 412) return setError(staleDeleteText("relationship", "remove"));
    if (failed) return setError(failed.detail);
    onDone("Relationship removed.");
  }
  return (
    <>
      <DialogHeader>
        <DialogTitle>Remove this relationship?</DialogTitle>
        <DialogDescription>{relationshipSentence(rel)}. The link and its confirmations are removed. The audit log keeps a record of it.</DialogDescription>
      </DialogHeader>
      <form onSubmit={submit} noValidate>
        {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
          <Button type="submit" variant="destructive" disabled={busy}>Remove relationship</Button>
        </DialogFooter>
      </form>
    </>
  );
}

// ---- Data flows ----------------------------------------------------------------------------------------------

const IconFlow = svg(<><path d="M2 5h10M9.5 2.5 12 5l-2.5 2.5" /><path d="M14 11H4M6.5 8.5 4 11l2.5 2.5" /></>);

/** Integrations in and out of this record. They are data flows, never operational dependencies, so they get their own
 *  section and the label says so. */
function DataFlows({ rec, write }: { rec: CatalogRecord; write: boolean }) {
  const out = useLoad<Page<Integration>>(`integrations?source=${rec.id}&page_size=200`);
  const into = useLoad<Page<Integration>>(`integrations?target=${rec.id}&page_size=200`);
  const reload = () => { out.reload(); into.reload(); };
  const error = out.error || into.error;
  const rows = out.data && into.data
    ? [
        ...out.data.results.map((i) => ({ i, dir: "out" as const })),
        ...into.data.results.map((i) => ({ i, dir: "in" as const })),
      ].sort((a, b) => a.i.name.localeCompare(b.i.name) || a.dir.localeCompare(b.dir))
    : null;
  return (
    <Panel id="h-rel-flows" icon={IconFlow} title="Data flows" count={rows?.length} note="data flow, not operational">
      {error ? <div className="px-4 pt-4"><ErrorBanner message={error} onRetry={reload} /></div>
        : !rows ? <div className="px-4 py-3"><Loading what="data flows" /></div>
        : rows.length === 0 ? (
          <div className="px-4 py-3">
            <p className="text-muted-foreground">No data flows are recorded for {rec.name}. A data flow moves data between records; it is not an operational dependency.</p>
            {write && <Button asChild size="sm" variant="outline" className="mt-3"><Link to="/integrations/new">Add integration</Link></Button>}
          </div>
        ) : (
          <TableRegion label="Data flows table">
            <Table>
              <TableCaption className="sr-only">Data flows into and out of {rec.name}, data flow not operational, sorted by integration name A to Z</TableCaption>
              <TableHeader><TableRow>
                <TableHead aria-sort="ascending">Integration</TableHead><TableHead>Direction</TableHead><TableHead>Link state</TableHead>
                <TableHead>Implemented by</TableHead><TableHead>Protocol</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map(({ i, dir }) => {
                  const other = dir === "out" ? i.target : i.source;
                  return (
                    <TableRow key={`${i.id}-${dir}`} className="h-[52px] align-top">
                      <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                        <Link to={`/integrations/${i.id}`}>{i.name}</Link>
                        {i.lifecycle === "retired" && <LifecycleChip value="retired" className="ml-2 align-middle" />}
                      </TableHead>
                      <TableCell className="whitespace-normal">
                        {dir === "out" ? "Sends data to " : "Receives data from "}
                        {/* Underlined: a link in running text isn't told apart by color alone (WCAG 1.4.1). */}
                        <RecordLink record={other} className="underline" />
                        {other.instance && <span className="text-muted-foreground"> ({other.instance.name})</span>}
                      </TableCell>
                      <TableCell className="whitespace-normal"><LinkStateChip state={i.link_state} /><span className="mt-1 block max-w-56 text-[13px] text-muted-foreground">{i.link_why}</span></TableCell>
                      <TableCell className="whitespace-normal">{i.vendor_native ? <Chip>Vendor-native</Chip> : i.implemented_by.map((c) => c.name).join(", ")}</TableCell>
                      <TableCell>{i.protocol || "—"}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableRegion>
        )}
    </Panel>
  );
}
