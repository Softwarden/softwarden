/** Relationship types, `/relationship-types`, loaded on first use (React.lazy in App.tsx).
 *
 *  Every role reads the types (they label every link); Admins add, change and delete them (the API's policy). A type
 *  reads from the source as its name ("Depends on") and from the target as its inverse name ("Is needed by"). The core
 *  types (depends_on, implements, replaces) can be renamed and described but never deleted, and their slug and whether
 *  they count as a dependency are fixed; no slug ever changes. A type in use can't be deleted: a row already known to be
 *  in use explains that, with the links that use it, instead of offering a delete that would fail; a 409 that arrives
 *  anyway (someone added a link meanwhile) shows the API's count. Search and filters are in the URL. */
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Lock } from "lucide-react";
import { api, isInUse, Problem, type Relationship, type RelationshipTypeDef } from "@/api";
import { Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, TableRegion, Textarea } from "@/components/app";
import { FilterBar } from "@/components/filter-bar";
import { SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Modal } from "@/features/admin/admin";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { pathOf } from "@/lib/kinds";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { inverseOf, phraseOf } from "@/lib/relationship-types";
import { useMe } from "@/lib/session";

const links = (n: number) => `${n} ${n === 1 ? "relationship" : "relationships"}`;
/** "1 relationship uses", "3 relationships use". */
const use = (n: number) => `${links(n)} ${n === 1 ? "uses" : "use"}`;
/** Everything that shows a type's labels: refreshed after a type changes or goes. */
const LABEL_PATHS = ["relationship-types", "relationships", "dependencies", "needs-attention"];
const ID = (k: string) => `rt-${k}`;
const NAME_MAX = 100;
const DESCRIPTION_MAX = 200;
const SLUG_MAX = 50;

const FILTERS = [
  { key: "dependency", label: "Dependency", options: [{ value: "yes", label: "Counts as a dependency" }, { value: "no", label: "Not a dependency" }] },
  { key: "core", label: "Origin", options: [{ value: "core", label: "Core" }, { value: "added", label: "Added by an Admin" }] },
];

/** Whether a type counts as a dependency, as a glyph plus text. */
function DependencyMark({ on }: { on: boolean }) {
  return on
    ? <span className="whitespace-nowrap"><span aria-hidden="true">→ </span>Dependency</span>
    : <span className="whitespace-nowrap text-muted-foreground"><span aria-hidden="true">○ </span>Not a dependency</span>;
}

function CoreChip() {
  return <Chip><Lock aria-hidden="true" className="size-3" />Core</Chip>;
}

export function RelationshipTypesPage() {
  const me = useMe();
  const admin = me.role === "admin";
  const [params, setParams] = useSearchParams();
  const values = { dependency: params.get("dependency") ?? "", core: params.get("core") ?? "" };
  const q = params.get("q") ?? "";
  const list = useApiQuery<RelationshipTypeDef[]>("relationship-types");
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState<RelationshipTypeDef | "new" | null>(null);
  const [deleting, setDeleting] = useState<RelationshipTypeDef | null>(null);
  const [notice, setNotice] = useState("");
  // The search and typed chips replace the history entry; a discrete filter change pushes one.
  const set = (k: string, v: string, typed = false) => setParams((cur) => { const n = new URLSearchParams(cur); if (v) n.set(k, v); else n.delete(k); return n; }, { replace: k === "q" || typed });
  const filtered = !!(values.dependency || values.core || q);
  const clear = () => setParams({});
  // After a delete the row, and the Delete button that opened the dialog, goes once the list refetches: then focus the
  // page heading rather than leaving focus on the body. `refocus` holds the list as it was when the delete finished, so
  // the check waits for the refetched list, and the flag is spent either way (focused, or the user moved focus first).
  const [refocus, setRefocus] = useState<{ before: unknown } | null>(null);
  useEffect(() => {
    if (!refocus || list.data === refocus.before) return;
    const t = setTimeout(() => {
      if (!document.activeElement || document.activeElement === document.body) document.querySelector<HTMLElement>("main h1")?.focus();
      setRefocus(null);
    }, 250);
    return () => clearTimeout(t);
  }, [refocus, list.data]);
  const done = (text: string, deleted = false) => {
    setEditing(null); setDeleting(null); setNotice(text);
    if (deleted) setRefocus({ before: list.data });
    void invalidate(...LABEL_PATHS);
  };
  const needle = q.trim().toLowerCase();
  const rows = list.data?.filter((t) =>
    (!needle || `${t.name} ${t.inverse_name} ${t.slug} ${t.description}`.toLowerCase().includes(needle))
    && (!values.dependency || t.is_dependency === (values.dependency === "yes"))
    && (!values.core || t.is_core === (values.core === "core")));

  return (
    <>
      <PageHead title="Relationship types" desc="The kinds of link between records. Each reads one way from the source and the other way from the target, for example Depends on and Is needed by.">
        {admin && <Button onClick={() => setEditing("new")}>Add relationship type</Button>}
      </PageHead>
      {!admin && <p className="mb-3 text-sm text-muted-foreground">You can view relationship types. Admins add, change and delete them.</p>}
      <FilterBar id="types" searchLabel="Search relationship types" searchPlaceholder="Name, inverse name, slug or description" search={q} onSearch={(v) => set("q", v)}
        fields={FILTERS} values={values} onChange={set} filtered={filtered} onClear={clear} />
      <SuccessNote text={notice} />
      {list.isError && <ErrorBanner message={`${problemText(list.error, "Couldn't load relationship types.")} Nothing was changed.`} onRetry={() => void list.refetch()} />}
      {list.isPending && <Loading what="relationship types" rows={4} />}
      {rows && rows.length === 0 && (filtered ? (
        <EmptyState title="No relationship types match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>Try another search, or clear the filters.</EmptyState>
      ) : (
        <EmptyState title="No relationship types">Every organization starts with Depends on, Implements and Replaces. If they're missing, ask an Admin to check the installation.</EmptyState>
      ))}
      {rows && rows.length > 0 && (
        <TableRegion label="Relationship types table">
          <Table>
            <TableCaption className="sr-only">Relationship types, by name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col" aria-sort="ascending">Name</TableHead><TableHead scope="col">Inverse name</TableHead><TableHead scope="col">Slug</TableHead>
              <TableHead scope="col">Dependency</TableHead><TableHead scope="col">Origin</TableHead><TableHead scope="col">Used by</TableHead>
              {admin && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((t) => (
                <TableRow key={t.id} className="align-top">
                  <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                    {t.name}
                    {t.description && <p className="mt-0.5 text-[13px] font-normal text-muted-foreground">{t.description}</p>}
                  </TableHead>
                  <TableCell className="whitespace-normal">{t.inverse_name || <span className="text-muted-foreground">Not set; reads “{inverseOf(t)}”</span>}</TableCell>
                  <TableCell><code className="font-mono text-[13px]">{t.slug}</code></TableCell>
                  <TableCell><DependencyMark on={t.is_dependency} /></TableCell>
                  <TableCell>{t.is_core ? <CoreChip /> : <span className="text-muted-foreground">Added</span>}</TableCell>
                  <TableCell className="tabular">{t.usage ? links(t.usage) : <span className="text-muted-foreground">Not used</span>}</TableCell>
                  {admin && (
                    <TableCell className="space-x-2 whitespace-nowrap text-right">
                      <Button size="sm" variant="outline" onClick={() => setEditing(t)}>Edit<span className="sr-only"> {t.name}</span></Button>
                      {t.is_core
                        ? <span className="text-[13px] text-muted-foreground">Core: can't be deleted</span>
                        : <Button size="sm" variant="outline" onClick={() => setDeleting(t)}>Delete<span className="sr-only"> {t.name}</span></Button>}
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      {rows && rows.length > 0 && <p className="mt-3 text-sm text-muted-foreground">Showing {rows.length} of {list.data!.length}. Sorted by name, A to Z. Core types can be renamed but not deleted.</p>}
      <Modal title={editing === "new" ? "Add relationship type" : `Edit ${editing ? editing.name : ""}`} open={editing !== null} onClose={() => setEditing(null)}
        description={editing === "new"
          ? "A new type takes the same ends as Depends on: applications, components and AI agents. Its slug can't change later."
          : editing && editing.is_core
            ? "A core type. Its name, inverse name and description can change; its slug and whether it counts as a dependency can't."
            : "The slug can't change. Saving relabels every link of this type."}>
        {editing === "new" && <TypeForm type={null} onCancel={() => setEditing(null)} onDone={done} />}
        {editing && editing !== "new" && (
          <OpenedEdit<RelationshipTypeDef> path={`relationship-types/${editing.id}`} what="relationship type">
            {(t, write) => <TypeForm type={t} write={write} onCancel={() => setEditing(null)} onDone={done} />}
          </OpenedEdit>
        )}
      </Modal>
      <Modal title={`Delete ${deleting?.name ?? ""}?`} open={deleting !== null} onClose={() => setDeleting(null)}
        description="Only a type no relationship uses can be deleted. Deleting it can't be undone; the audit log keeps a record.">
        {deleting && <DeleteTypeForm type={deleting} onClose={() => setDeleting(null)} onDone={(text) => done(text, true)} />}
      </Modal>
    </>
  );
}

// ---- add and edit ----------------------------------------------------------------------------------------------------------

/** A name as a slug, as the API derives it: "Feeds data to" → "feeds_data_to". */
export function slugOf(name: string) {
  const s = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, SLUG_MAX).replace(/_+$/, "");
  return /^[a-z]/.test(s) ? s : s ? `type_${s}`.slice(0, SLUG_MAX).replace(/_+$/, "") : "";
}

type FormState = { name: string; inverse_name: string; slug: string; description: string; is_dependency: boolean };

function TypeForm({ type, write, onCancel, onDone }: { type: RelationshipTypeDef | null; write?: OpenedWrite; onCancel: () => void; onDone: (text: string) => void }) {
  const [form, setForm] = useState<FormState>(() => ({
    name: type?.name ?? "", inverse_name: type?.inverse_name ?? "", slug: type?.slug ?? "", description: type?.description ?? "",
    is_dependency: type?.is_dependency ?? false,
  }));
  // The slug follows the name until it is typed in (create only: it never changes after).
  const [slugTyped, setSlugTyped] = useState(false);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const put = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((cur) => ({ ...cur, [k]: v }));
  const setName = (name: string) => setForm((cur) => ({ ...cur, name, slug: type || slugTyped ? cur.slug : slugOf(name) }));
  const core = !!type?.is_core;
  const usage = type?.usage ?? 0;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    const name = form.name.trim(), inverse = form.inverse_name.trim(), description = form.description.trim(), slug = form.slug.trim();
    if (!name) found.name = ["Enter a name, for example Feeds data to."];
    else if (name.length > NAME_MAX) found.name = [`Use ${NAME_MAX} characters or fewer.`];
    if (inverse.length > NAME_MAX) found.inverse_name = [`Use ${NAME_MAX} characters or fewer.`];
    if (description.length > DESCRIPTION_MAX) found.description = [`Use ${DESCRIPTION_MAX} characters or fewer.`];
    if (!type && slugTyped && slug && !/^[a-z][a-z0-9_]*$/.test(slug)) found.slug = ["Use lower-case letters, digits and underscores, starting with a letter."];
    if (Object.keys(found).length) { setErrors(Object.fromEntries(Object.entries(found).map(([k, v]) => [ID(k), v]))); return; }
    let body: Record<string, unknown>;
    if (!type) {
      // A slug left as derived isn't sent: the API derives it and reports a clash on the name, the field typed.
      body = { name, inverse_name: inverse, description, is_dependency: form.is_dependency, ...(slugTyped && slug ? { slug } : {}) };
    } else {
      // A PATCH sends only what changed; a core type never sends is_dependency.
      const next: Record<string, unknown> = { name, inverse_name: inverse, description, ...(core ? {} : { is_dependency: form.is_dependency }) };
      const before: Record<string, unknown> = { name: type.name, inverse_name: type.inverse_name, description: type.description, is_dependency: type.is_dependency };
      body = Object.fromEntries(Object.entries(next).filter(([k, v]) => v !== before[k]));
      if (!Object.keys(body).length) { onDone(`No changes to ${type.name}.`); return; }
    }
    setBusy(true);
    setErrors({});
    setFormError("");
    try {
      const saved = await api<RelationshipTypeDef>(type ? `relationship-types/${type.id}` : "relationship-types", { method: type ? "PATCH" : "POST", body, headers: await write?.ifMatch() });
      onDone(type ? `${saved.name} saved.` : `${saved.name} added.`);
    } catch (err) {
      setBusy(false);
      if (isStaleWrite(err) && write) write.onStale();
      else if (err instanceof Problem && Object.keys(err.errors).length) {
        const { non_field: general, ...rest } = err.errors;
        setErrors(Object.fromEntries(Object.entries(rest).map(([k, v]) => [ID(k), v])));
        if (general) setFormError(general.join(" "));
      } else setFormError(`${problemText(err, "Something went wrong.")} Nothing was saved.`);
    }
  }

  const previewName = form.name.trim() || "Name";
  const previewInverse = form.inverse_name.trim() || `${previewName} (from)`;
  return (
    <form onSubmit={submit} noValidate aria-label={type ? `Edit ${type.name}` : "Add relationship type"}>
      <fieldset disabled={busy} className="m-0 min-w-0 border-0 p-0">
        {formError && <ErrorBanner message={formError} />}
        <ErrorSummary errors={errors} focusOnShow="field" />
        <Field id={ID("name")} label="Name (required)" errors={errors} help="How a link reads from its source, for example Feeds data to. Unique in the organization.">
          <Input id={ID("name")} value={form.name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </Field>
        <Field id={ID("inverse_name")} label="Inverse name" errors={errors} help="How the same link reads from its target, for example Gets data from. Left blank, it reads as the name followed by “(from)”.">
          <Input id={ID("inverse_name")} value={form.inverse_name} onChange={(e) => put("inverse_name", e.target.value)} autoComplete="off" />
        </Field>
        <p className="-mt-1 mb-3 rounded-md border border-border bg-muted px-3 py-2 text-sm" aria-live="polite">
          <span className="font-semibold">Reads: </span>Portal {phraseOf(previewName)} Payroll Sync; Payroll Sync {phraseOf(previewInverse)} Portal.
        </p>
        {type ? (
          <Field id={ID("slug")} label="Slug" errors={errors} help="Can't be changed: links, filters and imports refer to it.">
            <Input id={ID("slug")} value={type.slug} readOnly className="font-mono" />
          </Field>
        ) : (
          <Field id={ID("slug")} label="Slug" errors={errors} help="Lower-case letters, digits and underscores, starting with a letter. Made from the name unless you type one. Can't be changed later.">
            <Input id={ID("slug")} value={form.slug} onChange={(e) => { setSlugTyped(true); put("slug", e.target.value); }} autoComplete="off" spellCheck={false} className="font-mono" maxLength={SLUG_MAX} />
          </Field>
        )}
        <Field id={ID("description")} label="Description" errors={errors} help={`Optional, up to ${DESCRIPTION_MAX} characters. Shown under the relationship picker.`}>
          <Textarea id={ID("description")} value={form.description} onChange={(e) => put("description", e.target.value)} rows={2} />
        </Field>
        {core ? (
          <Field id={ID("is_dependency")} label="Counts as a dependency" errors={errors}
            help="Fixed for core types: Depends on counts as a dependency; Implements and Replaces don't.">
            <Input id={ID("is_dependency")} value={type!.is_dependency ? "Yes" : "No"} readOnly />
          </Field>
        ) : (
          <div className="mb-3 flex flex-col gap-1.5">
            <label className="flex items-center gap-2 text-sm">
              <input id={ID("is_dependency")} type="checkbox" checked={form.is_dependency} onChange={(e) => put("is_dependency", e.target.checked)} className="h-4 w-4 accent-primary"
                aria-invalid={errors[ID("is_dependency")] ? true : undefined}
                aria-describedby={[errors[ID("is_dependency")] && `${ID("is_dependency")}-error`, `${ID("is_dependency")}-help`].filter(Boolean).join(" ")} />
              Counts as a dependency
            </label>
            <span id={`${ID("is_dependency")}-help`} className="text-sm text-muted-foreground">
              The dependency explorer, retirement checks and change assessments follow links of this type.
              {usage > 0 && ` ${use(usage)} it, so changing this marks the open change assessments they touch as stale.`}
            </span>
            {errors[ID("is_dependency")] && <span id={`${ID("is_dependency")}-error`} className="text-sm text-error-foreground">{errors[ID("is_dependency")].join(" ")}</span>}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
          <Button type="submit">{busy ? "Saving…" : type ? "Save" : "Add relationship type"}</Button>
        </DialogFooter>
      </fieldset>
    </form>
  );
}

// ---- delete ---------------------------------------------------------------------------------------------------------------

/** Delete an added type. In use already: says how many links use it and lists them (each opens its source record's
 *  Relationships tab), and offers no delete. Otherwise asks; a 409 that arrives anyway shows the API's count. */
function DeleteTypeForm({ type, onClose, onDone }: { type: RelationshipTypeDef; onClose: () => void; onDone: (text: string) => void }) {
  const [refused, setRefused] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const count = refused ?? type.usage;
  const blocked = count > 0;
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(`relationship-types/${type.id}`);
  async function remove() {
    setBusy(true);
    setError("");
    try {
      await api(`relationship-types/${type.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      onDone(`${type.name} deleted.`);
    } catch (e) {
      // In use only when the API counted the links; another 409 (a core type, say) is shown as the API worded it.
      if (isStaleWrite(e)) setError(staleDeleteText("relationship type"));
      else if (isInUse(e) && typeof e.blockingReferences.relationships === "number") setRefused(e.blockingReferences.relationships);
      else setError(`${problemText(e, "Couldn't delete the relationship type.")} Nothing was deleted.`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      {!blocked && <p className="mb-3">No relationship uses {type.name}.</p>}
      {blocked && (
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="alert" data-testid="delete-consequences">
          <p><span aria-hidden="true">⚠ </span>{type.name} is in use, so it can't be deleted: <strong>{use(count)}</strong> it. Remove those links, or change them to another type, then delete it.</p>
        </div>
      )}
      {blocked && <UsedBy type={type} />}
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        {!blocked && <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>{busy ? "Deleting…" : `Delete ${type.name}`}</Button>}
      </DialogFooter>
    </>
  );
}

const SHOWN = 20;

/** The links of a type, each in words from its source and linking to the source record's Relationships tab. Only the
 *  first 20 are fetched (`?limit=20`: `{results, count, has_more}`), with the total said as "20 of N". */
function UsedBy({ type }: { type: RelationshipTypeDef }) {
  const rels = useApiQuery<{ results: Relationship[]; count: number; has_more: boolean }>(`relationships?type=${encodeURIComponent(type.slug)}&limit=${SHOWN}`);
  if (rels.isPending) return <Loading what={`links that use ${type.name}`} />;
  if (rels.isError) return <ErrorBanner message={`${problemText(rels.error, "Couldn't load the links that use it.")}`} onRetry={() => void rels.refetch()} />;
  const rows = rels.data.results;
  if (!rows.length) return <p className="mb-3 text-sm text-muted-foreground">No link of this type is left. Close this and try again.</p>;
  const verb = phraseOf(type.name, type.slug);
  return (
    <div className="mb-3">
      <h3 className="mb-1 text-sm font-semibold" id="rt-used-by">Links that use {type.name}</h3>
      <ul aria-labelledby="rt-used-by" className="ml-5 list-disc text-sm">
        {rows.map((r) => {
          const to = pathOf(r.source, "relationships");
          const text = `${r.source.name} ${verb} ${r.target.name}`;
          return <li key={r.id}>{to ? <Link to={to} className="inline-block min-h-6 leading-6">{text}</Link> : text}</li>;
        })}
      </ul>
      {rels.data.has_more && <p className="mt-1 text-sm text-muted-foreground" data-testid="used-by-count">Showing {rows.length} of {rels.data.count} links.</p>}
    </div>
  );
}
