/** Reconciliation dialogs, loaded on first use from the Discovery queue, a record's Discovery sources
 *  panel and Needs attention:
 *
 *  - DriftDialog: "Source differs", per field: the record's value against the source's, since when, chosen fields
 *    applied (an ordinary audited record edit under the record's If-Match, with the source values the person reviewed
 *    as `seen`) or ignored (kept until the source changes again). 409 and 412 are said in words with a way to reload.
 *  - UndoDialog: undo a link, a create or a dismissal explicitly; a created record can also be archived, refused with
 *    the references that hold it in words.
 *  - MissingDialog: "No longer seen in source": keep the record (acknowledge, with an optional note) or archive it
 *    (confirmed, under the record's If-Match; refused while another source still sees it).
 *
 *  Discovery never changes a curated value on its own: every write here is a person's explicit, audited edit. */
import { useCallback, useEffect, useState } from "react";
import { api, apiWithEtag, Problem, when, type Drift, type ObservationDecision, type RecordRef } from "@/api";
import { ErrorSummary, Field, Textarea, TableRegion, useReturnFocus } from "@/components/app";
import { RecordLink } from "@/components/record-link";
import { chip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiPath, isRecordKind } from "@/lib/kinds";

const plural = (n: number, noun: string) => `${n} ${n === 1 || noun.endsWith("s") ? noun : `${noun}s`}`;

/** `blocking_references` {noun: count} in words: "1 licence pool and 2 relationships". */
export function referencesText(refs: Record<string, number>) {
  const parts = Object.entries(refs).map(([noun, n]) => plural(n, noun));
  return parts.length <= 1 ? parts.join("") : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

/** A refusal in words, with an optional way to reload what the dialog shows. */
function Refusal({ text, onReload, reloadLabel = "Load the latest version", children }: { text: string; onReload?: () => void; reloadLabel?: string; children?: React.ReactNode }) {
  return (
    <div role="alert" className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">
      <p className="[overflow-wrap:anywhere]">{text}</p>
      {children}
      {onReload && <Button type="button" size="sm" variant="outline" className="mt-2" onClick={onReload}>{reloadLabel}</Button>}
    </div>
  );
}

/** Split a 400's errors: those naming one of the dialog's inputs (shown in ErrorSummary, linked to the input) and
 *  the rest, said in words as a refusal (a link to an input that isn't there would lead nowhere). */
function splitErrors(e: Problem, inputs: string[]) {
  const fields = Object.fromEntries(Object.entries(e.errors).filter(([k]) => inputs.includes(k)));
  const other = Object.entries(e.errors).filter(([k]) => !inputs.includes(k)).flatMap(([, v]) => v);
  return { fields, other: other.length || !Object.keys(fields).length ? [e.detail, ...other].filter(Boolean).join(" ") : "" };
}

const Value = ({ v }: { v: string }) => (v ? <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{v}</span> : <span className="italic text-muted-foreground">Not set</span>);

// ---- source differs ---------------------------------------------------------------------------------------------------

/** The fields one source says differently from the record it is linked to. Editors and Admins choose fields and
 *  apply or ignore them; Viewers read them. */
export function DriftDialog({ observation, write, onClose, onDone }: {
  observation: string; write: boolean; onClose: () => void; onDone: (message: string) => void;
}) {
  const focus = useReturnFocus(true);
  const [drift, setDrift] = useState<{ data: Drift; etag: string | null } | null>(null);
  const [loadError, setLoadError] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [refusal, setRefusal] = useState<{ text: string; stale: boolean } | null>(null);
  const [busy, setBusy] = useState<"" | "apply" | "ignore">("");
  const load = useCallback(async () => {
    setLoadError("");
    try {
      const got = await apiWithEtag<Drift>(`discovery/observations/${observation}/drift`);
      setDrift(got);
      setChosen(got.data.differences.filter((d) => !d.ignored).map((d) => d.field));
    } catch (e) {
      setLoadError(e instanceof Problem ? e.detail : "Couldn't load what differs.");
    }
  }, [observation]);
  useEffect(() => { void load(); }, [load]);
  const d = drift?.data;
  const rows = d?.differences ?? [];
  const toggle = (f: string) => setChosen((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : [...cur, f]));
  // "Keep the record's values" acts on the chosen rows not already kept; with only kept ones chosen it has nothing to do.
  const keepable = rows.filter((r) => chosen.includes(r.field) && !r.ignored).map((r) => r.field);

  async function submit(action: "apply" | "ignore") {
    if (!d || busy) return;
    setErrors({});
    setRefusal(null);
    const wanted = action === "ignore" ? keepable : chosen;
    if (!wanted.length) {
      setErrors({ fields: [action === "ignore" ? "Choose at least one field whose record value isn't kept already." : "Choose at least one field to apply."] });
      return;
    }
    const fields = rows.filter((r) => wanted.includes(r.field));
    const body = { fields: fields.map((r) => r.field), seen: Object.fromEntries(fields.map((r) => [r.field, r.source])) };
    setBusy(action);
    try {
      await api(`discovery/observations/${observation}/drift/${action}`, {
        method: "POST", body, headers: action === "apply" ? { "If-Match": drift!.etag ?? `"${d.record_etag}"` } : undefined,
      });
      const names = fields.map((r) => r.label.toLowerCase()).join(", ");
      onDone(action === "apply"
        ? `Applied the ${names} from ${d.connector.name} to ${d.record.name}. The edit is in the record's history.`
        : `Kept ${d.record.name}'s ${names}. ${d.connector.name}'s value is hidden until it changes again.`);
    } catch (e) {
      if (!(e instanceof Problem)) setRefusal({ text: "Something went wrong. Nothing was changed; try again.", stale: false });
      else if (e.status === 412) setRefusal({ text: `Someone changed ${d.record.name} after you opened this, so nothing was applied. Load the latest version, check the values again, then apply.`, stale: true });
      else if (e.status === 409) setRefusal({ text: e.detail, stale: true });
      else {
        const split = splitErrors(e, ["fields"]);
        setErrors(split.fields);
        if (split.other) setRefusal({ text: split.other, stale: e.status === 400 && "seen" in e.errors });
      }
    } finally {
      setBusy("");
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl" {...focus}>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">{d ? `Source differs: ${d.record.name}` : "Source differs"}</DialogTitle>
          <DialogDescription>
            {d ? <>{d.connector.name} says {rows.length === 1 ? "this field" : "these fields"} differently from the record (as “{d.name}”). </> : null}
            {write
              ? "Applying a value is an ordinary edit of the record, recorded in its history. Ignoring keeps the record's value until the source says something new. Nothing is written back to the source."
              : "Editors and Admins can apply a source's value to the record or keep the record's. Nothing changes on its own."}
          </DialogDescription>
        </DialogHeader>
        {loadError && <Refusal text={`Couldn't load what differs. ${loadError}`} onReload={() => void load()} reloadLabel="Try again" />}
        {!d && !loadError && <p className="text-muted-foreground" aria-live="polite">Loading what differs…</p>}
        {d && (
          <form onSubmit={(e) => { e.preventDefault(); void submit("apply"); }} noValidate>
            <ErrorSummary errors={errors} />
            {refusal && <Refusal text={refusal.text} onReload={refusal.stale ? () => { setRefusal(null); void load(); } : undefined} />}
            {rows.length === 0 ? (
              <p className="mb-3 rounded-md border bg-muted px-3 py-2"><span aria-hidden="true">✓ </span>Nothing differs now: {d.record.name} matches what {d.connector.name} says.</p>
            ) : (
              <TableRegion label="Source differs table" className="mb-3">
                <Table>
                  <TableCaption className="sr-only">Fields {d.connector.name} says differently from {d.record.name}, with the record's value, the source's and since when</TableCaption>
                  <TableHeader><TableRow>
                    <TableHead scope="col">Field</TableHead><TableHead scope="col">This record</TableHead>
                    <TableHead scope="col">{d.connector.name} says</TableHead><TableHead scope="col">Since</TableHead><TableHead scope="col">State</TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {rows.map((r, i) => (
                      <TableRow key={r.field} className="align-top" data-field={r.field}>
                        <TableHead scope="row" className="whitespace-normal font-semibold text-foreground">
                          {write ? (
                            <label className="flex min-h-6 items-center gap-2">
                              <input type="checkbox" id={i === 0 ? "fields" : undefined} checked={chosen.includes(r.field)} onChange={() => toggle(r.field)} />
                              {r.label}
                            </label>
                          ) : r.label}
                        </TableHead>
                        <TableCell className="min-w-40 max-w-xs whitespace-normal"><Value v={r.curated} /></TableCell>
                        <TableCell className="min-w-40 max-w-xs whitespace-normal bg-state-pending text-state-pending-foreground"><Value v={r.source} /><span className="sr-only"> (differs)</span></TableCell>
                        <TableCell className="tabular whitespace-nowrap">{when(r.since)}</TableCell>
                        <TableCell className="whitespace-normal">
                          {r.ignored ? (
                            <>
                              <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>Record's value kept</span>
                              <span className="mt-1 block text-xs text-muted-foreground">{r.ignored_by?.name ?? "Someone"}, {when(r.ignored_at)}</span>
                            </>
                          ) : <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Differs</span>}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableRegion>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={onClose}>{write && rows.length ? "Cancel" : "Close"}</Button>
              {write && rows.length > 0 && <>
                <Button type="button" variant="outline" disabled={!!busy || !keepable.length} onClick={() => void submit("ignore")}>{busy === "ignore" ? "Saving…" : "Keep the record's values"}</Button>
                <Button type="submit" disabled={!!busy}>{busy === "apply" ? "Applying…" : "Apply the source's values"}</Button>
              </>}
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---- undo a decision --------------------------------------------------------------------------------------------------

export type UndoTarget = { id: string; name: string; connector: string; decision: Pick<ObservationDecision, "decision" | "reason" | "record"> };

/** Undo the candidate's decision. A created record stays unless "also archive" is chosen, which is refused (409, with
 *  what references it) while anything uses it or someone changed it since it was created. */
export function UndoDialog({ target, onClose, onDone }: { target: UndoTarget; onClose: () => void; onDone: (message: string) => void }) {
  const focus = useReturnFocus(true);
  const [archive, setArchive] = useState(false);
  const [refusal, setRefusal] = useState<{ text: string; refs: Record<string, number> } | null>(null);
  const [busy, setBusy] = useState(false);
  const { decision, reason, record } = target.decision;
  const created = decision === "created" && record !== null;
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setRefusal(null);
    try {
      await api(`discovery/observations/${target.id}/reopen`, { method: "POST", body: archive ? { archive: true } : {} });
      onDone(archive && record
        ? `Undone, and ${record.name} archived. ${target.name} is a candidate again; the record can be restored from its page.`
        : `Undone. ${target.name} is a candidate again${record ? `; ${record.name} stays as it is` : ""}.`);
    } catch (err) {
      if (err instanceof Problem) setRefusal({ text: [err.detail, ...Object.values(err.errors).flat()].filter((t, i, all) => t && all.indexOf(t) === i).join(" "), refs: err.blockingReferences });
      else setRefusal({ text: "Something went wrong. Nothing was undone; try again.", refs: {} });
    } finally {
      setBusy(false);
    }
  }
  const refs = refusal ? Object.entries(refusal.refs) : [];
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...focus}>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">Undo the decision on {target.name}</DialogTitle>
          <DialogDescription>
            {decision === "linked" && record && `${target.name} in ${target.connector} is unlinked from ${record.name} and becomes a candidate again. ${record.name} stays as it is.`}
            {created && `${target.name} becomes a candidate again. ${record!.name}, created from it, stays unless you also archive it.`}
            {decision === "dismissed" && `The dismissal${reason ? ` (“${reason}”)` : ""} is removed and ${target.name} becomes a candidate again.`}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate>
          {refusal && (
            <Refusal text={refusal.text}>
              {refs.length > 0 && (
                <>
                  <p className="mt-1 font-semibold">What still uses {record?.name ?? "it"}:</p>
                  <ul className="ml-5 list-disc">{refs.map(([noun, n]) => <li key={noun}>{plural(n, noun)}</li>)}</ul>
                </>
              )}
            </Refusal>
          )}
          {created && (
            <div className="mb-3">
              <label className="flex min-h-6 items-start gap-2 text-sm font-medium">
                <input type="checkbox" id="archive" className="mt-1" checked={archive} onChange={(e) => { setArchive(e.target.checked); setRefusal(null); }} aria-describedby="archive-help" />
                Also archive {record!.name}
              </label>
              <p id="archive-help" className="pl-6 text-sm text-muted-foreground">
                Only while nothing references it and nobody changed it since it was created from this candidate. An archived record can be restored from its page.
              </p>
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            {archive && record
              ? <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Undoing…" : `Undo and archive ${record.name}`}</Button>
              : <Button type="submit" disabled={busy}>{busy ? "Undoing…" : "Undo decision"}</Button>}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---- no longer seen in source -----------------------------------------------------------------------------------------

export type MissingTarget = { observation: string; sourceName: string; connector: string; since: string | null; record: RecordRef };

/** Keep the record (acknowledge) or archive it, for a linked record its source no longer lists. */
export function MissingDialog({ mode, target, onClose, onDone }: {
  mode: "acknowledge" | "archive"; target: MissingTarget; onClose: () => void; onDone: (message: string) => void;
}) {
  const focus = useReturnFocus(true);
  const { record } = target;
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [refusal, setRefusal] = useState<{ text: string; stale: boolean } | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const base = isRecordKind(record.kind) ? apiPath(record.kind) : null;
  const archiving = mode === "archive";
  /** The record's ETag for If-Match, or null (with the reason said) when it can't be read. */
  const loadTag = useCallback(async (): Promise<string | null> => {
    if (!archiving) return null;
    if (!base) {
      setRefusal({ text: `${record.name} can't be archived from here. Open it and archive it from its own page.`, stale: false });
      return null;
    }
    try {
      const got = (await apiWithEtag(`${base}/${record.id}`)).etag;
      setEtag(got);
      if (!got) setRefusal({ text: `Couldn't read ${record.name}'s version, so it can't be archived safely. Try again.`, stale: true });
      return got;
    } catch (e) {
      setRefusal({ text: e instanceof Problem ? e.detail : "Couldn't load the record.", stale: true });
      return null;
    }
  }, [archiving, base, record.id, record.name]);
  useEffect(() => { void loadTag(); }, [loadTag]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setErrors({});
    setRefusal(null);
    if (archiving && !confirmed) { setErrors({ confirm: [`Confirm that ${record.name} is to be archived.`] }); return; }
    setBusy(true);
    try {
      // Archiving is sent only with the record's ETag: read now if it hasn't loaded.
      const ifMatch = archiving ? (etag ?? (await loadTag())) : null;
      if (archiving && !ifMatch) return;
      await api(`discovery/observations/${target.observation}/missing/${mode}`, {
        method: "POST", body: note.trim() ? { note: note.trim() } : {}, headers: ifMatch ? { "If-Match": ifMatch } : undefined,
      });
      onDone(mode === "archive"
        ? `${record.name} archived. It can be restored from its page; the decision is in its history.`
        : `Kept ${record.name}. It's out of Needs attention until a source stops listing it again.`);
    } catch (err) {
      if (!(err instanceof Problem)) setRefusal({ text: "Something went wrong. Nothing was changed; try again.", stale: false });
      else if (err.status === 412) {
        setConfirmed(false);
        setEtag(null);
        setRefusal({ text: `Someone changed ${record.name} after you opened this, so it wasn't archived. Load the latest version and confirm again.`, stale: true });
      } else {
        const split = splitErrors(err, ["note"]);
        setErrors(split.fields);
        if (split.other) setRefusal({ text: split.other, stale: false });
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...focus}>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">{mode === "archive" ? `Archive ${record.name}` : `Keep ${record.name}`}</DialogTitle>
          <DialogDescription>
            {target.connector} no longer lists “{target.sourceName}”{target.since ? ` (since ${target.since})` : ""}, which is linked to <RecordLink record={record} className="underline" />.{" "}
            {mode === "archive"
              ? "Archiving takes the record out of the live catalog and Needs attention; it can be restored from its page. Refused while another source still sees it."
              : "Keeping it takes the row out of Needs attention and changes nothing on the record. If the source lists it again and later loses it, it comes back."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate>
          <ErrorSummary errors={errors} />
          {refusal && <Refusal text={refusal.text} onReload={refusal.stale ? () => { setRefusal(null); void loadTag(); } : undefined} />}
          <Field id="note" label="Note (optional)" errors={errors} help="Why, for the record's history: for example, moved to another tenant.">
            <Textarea id="note" rows={2} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
          {archiving && !etag && !refusal && <p id="archive-wait" className="mb-2 text-sm text-muted-foreground" aria-live="polite">Reading {record.name}'s current version…</p>}
          {archiving && (
            <div className="mb-3">
              <label className="flex min-h-6 items-center gap-2 text-sm font-medium">
                <input type="checkbox" id="confirm" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)}
                  aria-invalid={errors.confirm ? true : undefined} aria-describedby={errors.confirm ? "confirm-error" : undefined} />
                Archive {record.name}
              </label>
              {errors.confirm && <p id="confirm-error" className="mt-1 pl-6 text-sm text-error-foreground">{errors.confirm.join(" ")}</p>}
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            {mode === "archive"
              ? <Button type="submit" variant="destructive" disabled={busy || !etag} aria-describedby={!etag && !refusal ? "archive-wait" : undefined}>{busy ? "Archiving…" : `Archive ${record.name}`}</Button>
              : <Button type="submit" disabled={busy}>{busy ? "Saving…" : "Keep the record"}</Button>}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
