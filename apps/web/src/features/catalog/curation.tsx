import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, apiWithEtag, Problem, WEEKDAYS, type TagInfo } from "@/api";
import { apiPath, KIND_NOUN, KIND_PLURAL, listPath, type RecordKind } from "@/lib/kinds";
import { chip, GapChip, TagChip } from "@/components/sw";
import { apiKey, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import { saveDownload } from "@/lib/download";
import { ErrorBanner, LazyPart, lazyPart, Select, useLoad, useReturnFocus } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Curation tools for the Applications and Components lists: tags, saved filters and export. Bulk edit and bulk delete are in bulk.tsx. */

export type Tagged = { tags?: string[] };
export type { TagInfo, RecordKind };
export const MAX_BULK = 200;

// ---- Tags -----------------------------------------------------------------------------------------------------------

/** The organization's tags by lower-cased name, with their colour and description. Shared by every TagChips on the
 *  page (one request); while it loads, or if it fails, tags show as neutral chips. */
export function useTagInfo() {
  const tags = useApiQuery<TagInfo[]>("tags", { staleTime: 30_000 });
  return new Map((tags.data ?? []).map((t) => [t.name.toLowerCase(), t]));
}

/** Tags as chips in their colour, each showing its name (read only). */
export function TagChips({ tags, label = "Tags" }: { tags?: string[]; label?: string }) {
  const info = useTagInfo();
  if (!tags?.length) return null;
  return (
    <ul aria-label={label} className="m-0 flex list-none flex-wrap gap-1 p-0">
      {tags.map((t) => {
        const found = info.get(t.toLowerCase());
        return <li key={t} className="min-w-0 max-w-full"><TagChip name={t} color={found?.color} description={found?.description} /></li>;
      })}
    </ul>
  );
}

/** Tag input: type a name and press Enter or comma to add it; each tag is a chip with a Remove button.
 *  Suggestions come from the organization's existing tags. Tags are case-insensitive, so Finance and finance are one. */
export function TagInput({ id, label, tags, onChange, suggestions = [], help, errors, disabled }: {
  id: string; label: string; tags: string[]; onChange: (tags: string[]) => void; suggestions?: string[]; help?: string;
  errors?: string[]; disabled?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  const listId = useId();
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;

  function commit(names: string[]) {
    const cleaned = names.map((n) => n.trim().replace(/\s+/g, " ")).filter(Boolean);
    const next = [...tags];
    const added: string[] = [];
    for (const n of cleaned) {
      if (!next.some((t) => t.toLowerCase() === n.toLowerCase())) { next.push(n); added.push(n); }
    }
    setDraft("");
    if (added.length) { onChange(next); setNote(`Added ${added.join(", ")}.`); }
    else if (cleaned.length) setNote(`${cleaned[0]} is already added.`);
  }
  function remove(tag: string) {
    onChange(tags.filter((t) => t !== tag));
    setNote(`Removed ${tag}.`);
    setTimeout(() => document.getElementById(id)?.focus());
  }
  function onText(value: string) {
    if (!value.includes(",")) return setDraft(value);
    const parts = value.split(",");
    commit(parts.slice(0, -1));
    setDraft(parts[parts.length - 1]);
  }
  const offered = suggestions.filter((s) => !tags.some((t) => t.toLowerCase() === s.toLowerCase()));
  return (
    <div className="mb-3 flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {tags.length > 0 && (
        <ul aria-label={`${label} added`} className="m-0 flex list-none flex-wrap gap-1.5 p-0">
          {tags.map((t) => (
            <li key={t}>
              <span className="inline-flex items-center gap-0.5 rounded-full border border-border-strong bg-muted py-0 pl-2.5 pr-0.5 text-[13px]">
                {t}
                <button type="button" disabled={disabled} aria-label={`Remove tag ${t}`} onClick={() => remove(t)}
                  className="inline-flex h-6 min-w-6 items-center justify-center rounded-full text-muted-foreground outline-none hover:bg-accent hover:text-accent-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50">
                  <span aria-hidden="true">×</span>
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <Input id={id} value={draft} list={listId} autoComplete="off" disabled={disabled} placeholder="Type a tag, then press Enter"
        aria-invalid={errors?.length ? true : undefined} aria-describedby={[errors?.length && errorId, helpId].filter(Boolean).join(" ")}
        onChange={(e) => onText(e.target.value)}
        onBlur={() => draft.trim() && commit([draft])}
        onKeyDown={(e) => { if (e.key === "Enter" && draft.trim()) { e.preventDefault(); commit([draft]); } }} />
      <datalist id={listId}>{offered.map((s) => <option key={s} value={s} />)}</datalist>
      <span className="text-sm text-muted-foreground" id={helpId}>{help ?? "Press Enter or comma to add. Tags are not case-sensitive."}</span>
      {errors?.length ? <span className="text-sm text-error-foreground" id={errorId}>{errors.join(" ")}</span> : null}
      <p className="sr-only" role="status">{note}</p>
    </div>
  );
}

/** The organization's tag names, for suggestions. Fails quietly: the input still works without them. */
export function useTagNames() {
  const tags = useLoad<TagInfo[]>("tags");
  return tags.data?.map((t) => t.name) ?? [];
}

// ---- Saved views ----------------------------------------------------------------------------------------------------------

export type Query = Record<string, string>;
/** A saved view (`saved-filters`): a list's filters and, when
 *  not empty, the columns to show. Built-ins (`builtin:…` IDs) live in the API's code and can't be changed. */
export type SavedFilter = {
  id: string; name: string; kind: RecordKind; query: Query; shared: boolean; builtin: boolean; mine: boolean; can_delete: boolean;
  /** The SPA's column IDs, in order; empty means the list's defaults. Absent from an older API. */
  columns?: string[];
  owner?: { id: number; name: string } | null;
  can_edit?: boolean;
  /** What the list would refuse in the query now, as "parameter: message" (a custom field deleted or changed since). */
  problems?: string[];
  /** The requester's own weekly email of this view. */
  subscription?: { id: string; weekday: number; active: boolean } | null;
};

/** The columns a list has, which show now, and which show by default (all by column ID). */
export type ViewColumns = { all: { id: string; header: string }[]; shown: string[]; defaults: string[] };

/** A saved filter's problem split into the parameter and what is wrong with it, in words. A `cf_` parameter the list no
 *  longer takes refers to a custom field that was deleted or no longer applies here; a parameter outside the saved-view
 *  allow-list is one the list page doesn't apply. */
export function filterProblem(text: string): { param: string; message: string } {
  const at = text.indexOf(": ");
  const param = at > 0 ? text.slice(0, at) : "";
  const raw = at > 0 ? text.slice(at + 2) : text;
  const message = param.startsWith("cf_") && raw === "This filter isn't available here."
    ? "refers to a custom field that no longer exists or no longer applies here."
    : raw.startsWith("A saved view can't hold this filter")
      ? "isn't a filter the list page applies, so a saved view can't keep it. Remove it and save again."
      : raw;
  return { param, message };
}

/** A problem as one sentence: "cf_tier refers to a custom field …". */
export const problemLine = (text: string) => {
  const { param, message } = filterProblem(text);
  return param ? `${param} ${message}` : message;
};

/** Order-independent comparison of two list queries, ignoring empty values. */
export function sameQuery(a: Query, b: Query) {
  const norm = (q: Query) => JSON.stringify(Object.entries(q).filter(([, v]) => v !== "").sort(([x], [y]) => x.localeCompare(y)));
  return norm(a) === norm(b);
}

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

/** The columns a view shows: its own that the list still has (the name first), or the list's defaults. */
export function viewColumns(view: SavedFilter, columns: ViewColumns): string[] {
  const own = (view.columns ?? []).filter((c) => columns.all.some((x) => x.id === c));
  return own.length ? ["name", ...own.filter((c) => c !== "name")] : columns.defaults;
}

/** The columns to store for what shows now: none (the list's defaults) when the defaults show, else the ones shown. */
export function columnsToSave(columns: ViewColumns): string[] {
  return sameSet(columns.shown, columns.defaults) ? [] : columns.shown;
}

/** The list page that opens a view: its route and `view=<id>` (the page applies the view's query). */
export function viewPath(kind: RecordKind, id: string) {
  const base = listPath(kind);
  return `${base}${base.includes("?") ? "&" : "?"}view=${encodeURIComponent(id)}`;
}

const QUERY_LABEL: Record<string, string> = {
  lifecycle: "Lifecycle", archived: "Archived", gap: "Gaps", review_state: "Review state", imported: "Imported", criticality: "Criticality",
  component_type: "Type", tag: "Tag", q: "Search",
  category: "Category", deployment_model: "Deployment model", data_classification: "Data classification", department: "Department",
  hosting: "Hosting", environment: "Environment", platform: "Platform", channel: "Channel", risk_classification: "Risk classification",
  human_in_loop: "Human in the loop", actions_can_write: "Actions can write", model: "Model", provider: "Provider",
  sso_enabled: "SSO", scim_provisioning: "SCIM", authentication_method: "Authentication",
  owner: "Owner", support_team: "Support team", it_steward: "IT steward", periodic_review_due: "Periodic review due",
  is_gxp: "GxP", validation_status: "Validation status", risk_tier: "Risk tier",
};
const QUERY_VALUE: Record<string, string> = { "active,deprecated": "Active, Deprecated", true: "yes", false: "no" };
/** "Lifecycle: Active, Deprecated; Archived: no; Tag: Finance" */
/** `names` gives the words for values the query holds as IDs or codes ("category=<id>" → the category's name). */
export function describeQuery(q: Query, names: Record<string, string> = {}) {
  // A key alone in `names` is a parameter's label (custom field filters: "cf_tier" → "Tier").
  const parts = Object.entries(q).filter(([, v]) => v !== "").map(([k, v]) => `${QUERY_LABEL[k] ?? names[k] ?? k}: ${names[`${k}=${v}`] ?? QUERY_VALUE[v] ?? (k.startsWith("cf_") ? v : v.replace(/_/g, " "))}`);
  return parts.length ? parts.join("; ") : "No filters (all records)";
}

/** "Name, Lifecycle, Review state" for column IDs. */
export function describeColumns(ids: string[], columns: ViewColumns) {
  return ids.map((id) => columns.all.find((c) => c.id === id)?.header ?? id).join(", ");
}

const isStoredView = (id: string) => /^[0-9a-f-]{36}$/i.test(id);

// The dialogs load on first use (saved-views.tsx), so the list's first bundle stays small.
const SaveViewDialog = lazyPart(() => import("@/features/catalog/saved-views").then((m) => m.SaveViewDialog));
const EditViewDialog = lazyPart(() => import("@/features/catalog/saved-views").then((m) => m.EditViewDialog));
const DeleteViewDialog = lazyPart(() => import("@/features/catalog/saved-views").then((m) => m.DeleteViewDialog));
const SubscriptionDialog = lazyPart(() => import("@/features/catalog/saved-views").then((m) => m.SubscriptionDialog));
const SubscribersDialog = lazyPart(() => import("@/features/catalog/saved-views").then((m) => m.SubscribersDialog));

type ViewDialog = "save" | "edit" | "delete" | "email" | "subscribers";

/** Saved views on a record list: choose one (its query and columns apply; `?view=<id>` in the URL marks it
 *  open), save the current filters and columns as a view, and for the open view: save changes to it, rename or share
 *  it, delete it, and its weekly email. Writes to a stored view send If-Match from the copy read when it opened (412:
 *  load the latest version). */
export function SavedViews({ kind, query, viewId, columns, columnsReady, bare, canShare, onOpen, onClose, onColumns, onNotice, names }: {
  kind: RecordKind; query: Query; viewId: string; columns: ViewColumns; canShare: boolean;
  /** Every column the list can have is known (custom fields loaded): a view's columns can be applied. */
  columnsReady: boolean;
  /** The URL names a view and no filters (`?view=<id>` alone, as the profile links): the view's query is applied. */
  bare: boolean;
  /** Make the view's columns the table's column choice; called once each time a view opens. */
  onColumns: (columns: string[]) => void;
  /** Open a view: its query in the URL with `preset=all` (and `view=<id>` for a stored view); `query` replaces the
   *  view's own (a view opened without the parameters the list refuses); `replace` replaces the history entry (a bare
   *  `?view=<id>` link, so Back doesn't return to a URL whose rows aren't the view's). */
  onOpen: (view: SavedFilter, query?: Query, replace?: boolean) => void;
  /** The open view is gone (deleted): drop `view` from the URL, keeping the filters. */
  onClose: () => void;
  onNotice: (text: string) => void;
  /** Words for coded values in the query, "key=value" → label (see describeQuery). */
  names?: Record<string, string>;
}) {
  const me = useMe();
  const filters = useLoad<SavedFilter[]>(`saved-filters?kind=${kind}`, { strict: true });
  const stored = isStoredView(viewId);
  // The open view as read when it opened, with its ETag: changes send it back as If-Match.
  const detailPath = `saved-filters/${viewId}`;
  const detail = useQuery({
    queryKey: [...apiKey(detailPath), "etag"], queryFn: () => apiWithEtag<SavedFilter>(detailPath),
    enabled: stored, gcTime: 0, staleTime: Infinity, retry: false,
  });
  const [dialog, setDialog] = useState<ViewDialog | null>(null);
  const [used, setUsed] = useState<Set<ViewDialog>>(new Set());
  const open = (d: ViewDialog) => { setUsed((u) => new Set(u).add(d)); setDialog(d); };
  const onOpenChange = (d: ViewDialog) => (o: boolean) => setDialog(o ? d : null);
  // A saved view the list would refuse as it is: its problems are shown before anything is applied.
  const [troubled, setTroubled] = useState<SavedFilter | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ stale: boolean; lines: string[] } | null>(null);
  // Views deleted here are left out at once, so a deleted name never shows while the listing reloads.
  const [deleted, setDeleted] = useState<Set<string>>(new Set());
  const list = (filters.data ?? []).filter((f) => !deleted.has(f.id));
  const missing = stored && detail.isError && detail.error instanceof Problem && detail.error.status === 404;
  const loaded = stored && detail.data?.data.id === viewId ? detail.data.data : undefined;
  // A view of another list (a components view's ID on Applications): not opened here; a link says where it opens.
  const elsewhere = loaded && loaded.kind !== kind ? loaded : undefined;
  const unavailable = missing;
  // The selector's value: the stored view the URL names, else a view whose query is the list's (built-ins included).
  const selected = stored ? (loaded && !elsewhere ? loaded : undefined) : viewId ? list.find((f) => f.id === viewId) : list.find((f) => sameQuery(f.query, query));
  // A stored view whose query the list's filters equal, without `view=` in the URL (filters set by hand, or a link
  // without it): the selector shows it, and choosing it again changes nothing, so "Open this view" opens it.
  const matched = !viewId && selected && !selected.builtin ? selected : undefined;
  // The open view's region and its writes: only a stored view the URL names, of this list, read with its ETag.
  const view = loaded && !elsewhere ? loaded : null;
  // Its columns apply once each time it opens; until then the comparison would be with the user's own columns.
  const [applied, setApplied] = useState("");
  const queryChanged = !!view && !sameQuery(view.query, query);
  const columnsChanged = !!view && applied === viewId && !sameSet(viewColumns(view, columns), columns.shown);
  const changed = applied === viewId && (queryChanged || columnsChanged);
  useEffect(() => { setSaveError(null); }, [viewId]);
  // `?view=<id>` alone opens the view: its query goes into the URL, replacing the history entry (a view with problems
  // asks first). A built-in's ID opens it by its query.
  const bareView = stored ? view : viewId ? list.find((f) => f.id === viewId) : undefined;
  const chosen = useRef("");
  useEffect(() => {
    if (!bare || !bareView || chosen.current === bareView.id) return;
    chosen.current = bareView.id;
    if (bareView.problems?.length) setTroubled(bareView);
    else onOpen(bareView, bareView.query, true);
  }, [bare, bareView]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!viewId) { setApplied(""); return; }
    if (!view || !columnsReady || applied === viewId) return;
    setApplied(viewId);
    onColumns(view.columns ?? []);
  }, [viewId, view, columnsReady, applied]); // eslint-disable-line react-hooks/exhaustive-deps
  /** Back to the view as saved: its query and its columns. */
  function reopen() {
    if (!view) return;
    setApplied("");
    onOpen(view);
  }

  const group = (title: string, items: SavedFilter[]) => items.length > 0 && (
    <optgroup label={title}>{items.map((f) => <option key={f.id} value={f.id}>{f.name}{f.problems?.length ? " (needs fixing)" : ""}</option>)}</optgroup>
  );
  function choose(f: SavedFilter) {
    if (f.problems?.length) { setTroubled(f); return; }
    setTroubled(null);
    onOpen(f);
  }
  /** Open the view without the parameters the list refuses; "Save changes" then stores it that way. */
  function openWithout(f: SavedFilter) {
    const bad = new Set((f.problems ?? []).map((p) => filterProblem(p).param));
    const kept = Object.fromEntries(Object.entries(f.query).filter(([k]) => !bad.has(k)));
    setTroubled(null);
    onOpen(f, kept);
    onNotice(`Opened “${f.name}” without ${[...bad].join(", ")}. ${f.can_edit ? "Save changes to the view to keep it this way." : "Save it as a view of your own to keep it this way."}`);
    setTimeout(() => document.getElementById("saved-filter")?.focus());
  }
  function refresh() {
    filters.reload();
    if (stored) void detail.refetch();
  }
  /** Store the filters and columns shown now in the open view (If-Match: the copy read when it opened). */
  async function saveChanges() {
    if (!view) return;
    setSaving(true);
    setSaveError(null);
    const etag = detail.data?.etag;
    try {
      await api(`saved-filters/${view.id}`, { method: "PATCH", body: { query, columns: columnsToSave(columns) }, headers: etag ? { "If-Match": etag } : undefined });
      onNotice(`Saved changes to “${view.name}”.`);
      refresh();
    } catch (e) {
      if (e instanceof Problem && e.status === 412) setSaveError({ stale: true, lines: [] });
      else if (e instanceof Problem) {
        const lines = [...(e.errors.query ?? []).map(problemLine), ...(e.errors.columns ?? [])];
        setSaveError({ stale: false, lines: lines.length ? lines : [e.detail] });
      } else setSaveError({ stale: false, lines: ["Couldn't save the view. Nothing was changed. Try again."] });
    } finally {
      setSaving(false);
    }
  }
  const admin = me.role === "admin";
  const sub = view?.subscription ?? null;
  const deleting = troubled ?? view;
  return (
    <div className="mb-4 flex flex-wrap items-end gap-x-3 gap-y-2">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="saved-filter">Saved views</Label>
        <Select id="saved-filter" className="min-w-56" value={selected?.id ?? ""} disabled={!filters.data}
          onChange={(e) => { const f = list.find((x) => x.id === e.target.value); if (f) choose(f); }}>
          <option value="" disabled>{filters.data ? "Choose a saved view…" : "Loading saved views…"}</option>
          {group("Built-in", list.filter((f) => f.builtin))}
          {group("Shared with everyone", list.filter((f) => !f.builtin && f.shared))}
          {group("My views", list.filter((f) => !f.builtin && !f.shared))}
          {/* The open view isn't listed until the listing refreshes (just saved, or just shared with you). */}
          {selected && !list.some((f) => f.id === selected.id) && <option value={selected.id}>{selected.name}</option>}
        </Select>
      </div>
      {matched && (
        <Button variant="outline" size="sm" aria-describedby="saved-filter-matched" onClick={() => choose(matched)}>Open this view</Button>
      )}
      <Button variant="outline" size="sm" onClick={() => open("save")}>Save as view…</Button>
      {matched && (
        <p id="saved-filter-matched" className="w-full text-sm text-muted-foreground" data-testid="view-matched">
          These filters are the same as “{matched.name}”. Open it to see its columns, email and changes since it was saved.
        </p>
      )}
      {filters.error && <span role="alert" className="text-sm text-error-foreground">Couldn't load saved views. <button type="button" className="underline" onClick={filters.reload}>Retry</button></span>}
      {unavailable && (
        <p className="w-full rounded-md border bg-card px-3 py-2 text-sm" data-testid="view-unavailable">
          <span aria-hidden="true">○ </span>The saved view in this link isn't available to you: it was deleted, or it is private to the person who saved it. The list shows the filters in the link.
        </p>
      )}
      {elsewhere && (
        <p className="w-full rounded-md border bg-card px-3 py-2 text-sm" data-testid="view-elsewhere">
          <span aria-hidden="true">○ </span>“{elsewhere.name}” is a view of {KIND_PLURAL[elsewhere.kind].toLowerCase()}, so it isn't opened here. <Link to={viewPath(elsewhere.kind, elsewhere.id)}>Open it on {KIND_PLURAL[elsewhere.kind]}</Link>
        </p>
      )}
      {stored && detail.isError && !unavailable && (
        <span role="alert" className="text-sm text-error-foreground">Couldn't load the saved view. <button type="button" className="underline" onClick={() => void detail.refetch()}>Retry</button></span>
      )}
      {view && (
        <section aria-labelledby="open-view-name" className="w-full rounded-md border bg-card px-3 py-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <h2 id="open-view-name" className="text-sm font-semibold">View: {view.name}</h2>
            <span className={chip({ state: "neutral" })}>{view.shared ? "Shared with everyone" : "Only you"}</span>
            {view.shared && (view.owner ? <span className="text-sm text-muted-foreground">Owner: {view.owner.name}</span> : <GapChip label="No owner" />)}
            {(view.problems?.length ?? 0) > 0 && <GapChip label="Filter needs fixing" />}
            {changed && <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Changed since saved: {queryChanged && columnsChanged ? "filters and columns" : queryChanged ? "filters" : "columns"}</span>}
            {sub && <span className={chip({ state: "neutral" })}><span aria-hidden="true">✉</span>Weekly email: {WEEKDAYS[sub.weekday]}s{sub.active ? "" : " (turned off)"}</span>}
          </div>
          {(view.problems?.length ?? 0) > 0 && (
            <ul className="ml-5 mt-1 list-disc text-sm">
              {(view.problems ?? []).map((p) => { const { param, message } = filterProblem(p); return <li key={p}>{param && <><code className="font-mono text-[13px]">{param}</code> </>}{message}</li>; })}
            </ul>
          )}
          {!view.can_edit && view.shared && <p className="mt-1 text-sm text-muted-foreground">Only its owner (an Editor or Admin) or an Admin can change this shared view. Save as view to keep a copy of your own.</p>}
          {saveError && (
            <div role="alert" className="mt-2 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm">
              {saveError.stale ? (
                <span className="flex flex-wrap items-center gap-2">
                  <strong className="min-w-0 flex-1">Someone else changed this view after you opened it, so your changes weren't saved. Load the latest version, then save again.</strong>
                  <Button type="button" size="sm" variant="outline" onClick={() => { setSaveError(null); refresh(); }}>Load the latest version</Button>
                </span>
              ) : (
                <><strong>The view wasn't saved:</strong><ul className="ml-5 list-disc">{saveError.lines.map((l) => <li key={l}>{l}</li>)}</ul></>
              )}
            </div>
          )}
          <div className="mt-2 flex flex-wrap gap-2">
            {view.can_edit && changed && <Button size="sm" variant="outline" disabled={saving || !detail.data?.etag} onClick={() => void saveChanges()}>{saving ? "Saving…" : `Save changes to “${view.name}”`}</Button>}
            {changed && <Button size="sm" variant="outline" onClick={reopen}>Reopen saved version</Button>}
            {view.can_edit && <Button size="sm" variant="outline" onClick={() => open("edit")}>Rename or share…</Button>}
            <Button size="sm" variant="outline" onClick={() => open("email")}>{sub ? "Weekly email…" : "Email me weekly…"}</Button>
            {admin && view.shared && <Button size="sm" variant="outline" onClick={() => open("subscribers")}>Subscribers…</Button>}
            {view.can_delete && <Button size="sm" variant="outline" onClick={() => open("delete")}>Delete view…</Button>}
          </div>
        </section>
      )}
      {troubled && (
        <div role="alert" aria-labelledby="saved-filter-problem" className="w-full rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground">
          <p id="saved-filter-problem" className="font-semibold"><span aria-hidden="true">⚠ </span>“{troubled.name}” can't be opened as it was saved:</p>
          <ul className="ml-5 mt-1 list-disc">
            {(troubled.problems ?? []).map((p) => { const { param, message } = filterProblem(p); return <li key={p}>{param && <><code className="font-mono text-[13px]">{param}</code> </>}{message}</li>; })}
          </ul>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => openWithout(troubled)}>
              Open without {(troubled.problems ?? []).map((p) => filterProblem(p).param).filter(Boolean).join(", ") || "the problem"}
            </Button>
            {troubled.can_delete && <Button size="sm" variant="outline" onClick={() => open("delete")}>Delete “{troubled.name}”…</Button>}
            <Button size="sm" variant="outline" onClick={() => { setTroubled(null); setTimeout(() => document.getElementById("saved-filter")?.focus()); }}>Cancel</Button>
          </div>
        </div>
      )}
      {used.has("save") && (
        <LazyPart what="saving a view">
          <SaveViewDialog open={dialog === "save"} onOpenChange={onOpenChange("save")} kind={kind} query={query} columns={columns} canShare={canShare} names={names}
            onSaved={(f) => { filters.reload(); onOpen(f); onNotice(`Saved view “${f.name}”${f.shared ? ", shared with everyone" : ""}.`); }} />
        </LazyPart>
      )}
      {used.has("edit") && view && (
        <LazyPart what="editing the view">
          <EditViewDialog open={dialog === "edit"} onOpenChange={onOpenChange("edit")} view={view} etag={detail.data?.etag ?? null} canShare={canShare}
            names={names} columns={columns} onReload={() => void detail.refetch()}
            onSaved={(f) => { refresh(); onNotice(`Saved “${f.name}”, ${f.shared ? "shared with everyone" : "only you can see it"}.`); }} />
        </LazyPart>
      )}
      {used.has("delete") && deleting && (
        <LazyPart what="deleting the view">
          <DeleteViewDialog open={dialog === "delete"} onOpenChange={onOpenChange("delete")} view={deleting} etag={deleting.id === view?.id ? detail.data?.etag ?? null : null}
            onDeleted={(f) => {
              setTroubled(null);
              setDeleted((d) => new Set(d).add(f.id));
              if (f.id === viewId) onClose();
              filters.reload();
              onNotice(`Deleted saved view “${f.name}”.`);
            }} />
        </LazyPart>
      )}
      {used.has("email") && view && (
        <LazyPart what="the weekly email">
          <SubscriptionDialog open={dialog === "email"} onOpenChange={onOpenChange("email")} view={view}
            onChanged={(text) => { refresh(); onNotice(text); }} />
        </LazyPart>
      )}
      {used.has("subscribers") && view && admin && (
        <LazyPart what="the view's subscribers">
          <SubscribersDialog open={dialog === "subscribers"} onOpenChange={onOpenChange("subscribers")} view={view} onChanged={() => refresh()} />
        </LazyPart>
      )}
    </div>
  );
}

// ---- Export ------------------------------------------------------------------------------------------------------------------

const qs = (q: Query) => new URLSearchParams(Object.entries(q).filter(([, v]) => v !== "")).toString();

/** The CSV template link target (Editors and Admins). */
export const templateUrl = (kind: RecordKind) => `/api/v1/${apiPath(kind)}/template.csv`;

export type ExportFormat = "csv" | "xlsx";
const FORMAT_NAME: Record<ExportFormat, string> = { csv: "CSV", xlsx: "Excel" };

/** Export CSV or Excel. The kind is the list you are on; the format is
 *  the button that opened it. The file is fetched so a failure can be shown here. */
export function ExportDialog({ open, onOpenChange, kind, query, emptyList, onDone, format = "csv", names }: {
  open: boolean; onOpenChange: (o: boolean) => void; kind: RecordKind; query: Query; emptyList: boolean; onDone: (text: string) => void;
  format?: ExportFormat; names?: Record<string, string>;
}) {
  const returnFocus = useReturnFocus(open);
  const [rows, setRows] = useState<"current" | "all">("current");
  const [archived, setArchived] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const plural = apiPath(kind);
  const noun = KIND_NOUN[kind][1];
  useEffect(() => { if (open) { setRows("current"); setArchived(false); setError(""); } }, [open]);

  async function download() {
    const params: Query = rows === "current" ? { ...query } : {};
    if (archived) {
      if (params.archived === "false") delete params.archived; // include_archived adds the archived records back
      params.include_archived = "true";
    }
    setBusy(true);
    setError("");
    try {
      const name = await saveDownload(`${plural}/export.${format}?${qs(params)}`, `softwarden-${plural}.${format}`);
      onOpenChange(false);
      onDone(`Exported ${noun} to ${name}. The export is recorded in the audit log.`);
    } catch (e) {
      setError(`${e instanceof Error ? e.message : "The export failed."} Nothing was downloaded. Try again.`);
    } finally {
      setBusy(false);
    }
  }
  const noRows = rows === "current" && emptyList && !archived;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Export {FORMAT_NAME[format]}</DialogTitle>
          <DialogDescription>{format === "csv"
            ? `Download ${noun} as a CSV file with a Softwarden ID column, so it can be imported again.`
            : `Download ${noun} as an Excel workbook with a frozen, filtered header row, typed dates and yes/no columns.`}</DialogDescription>
        </DialogHeader>
        {error && <ErrorBanner message={error} />}
        <p className="text-sm"><span className="font-medium">Kind:</span> {KIND_PLURAL[kind]}</p>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1.5 text-sm font-medium">Rows</legend>
          <label className="flex items-start gap-2 text-sm">
            <input type="radio" name="export-rows" checked={rows === "current"} onChange={() => setRows("current")} className="mt-0.5 h-4 w-4 accent-primary" />
            <span>Current filters<span className="block text-muted-foreground">{describeQuery(query, names)}</span></span>
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="radio" name="export-rows" checked={rows === "all"} onChange={() => setRows("all")} className="h-4 w-4 accent-primary" />
            All {noun}
          </label>
        </fieldset>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} className="h-4 w-4 accent-primary" />
          Include archived
        </label>
        <p className="text-sm text-muted-foreground">
          {format === "csv"
            ? "Cells that start with = + - or @ are prefixed with a ' so spreadsheet apps don't run them; the file is UTF-8 with a BOM."
            : "Text cells are stored as text, never as formulas, and cells that start with = + - or @ are prefixed with a '."}
          {" "}The export is recorded in the audit log.
        </p>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="button" disabled={busy || noRows} onClick={() => void download()}>
            {busy ? "Preparing…" : noRows ? "No rows to export" : `Download ${FORMAT_NAME[format]}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// Bulk edit and bulk delete are in bulk.tsx, loaded on first use.
