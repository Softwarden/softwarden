import { useState } from "react";
import { api, apiWithEtag, AttentionQueue, orgToday, Problem, Snooze, type ReminderSourceType, type ReminderStatus } from "@/api";
import { useMe } from "@/lib/session";
import { ErrorSummary, Field, Textarea, useReturnFocus } from "@/components/app";
import { MoreMenu } from "@/components/more-menu";
import { chip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ANY_REVISION, ifMatch, isStaleWrite, useOpenedRecord } from "@/lib/if-match";

/** Snooze and acknowledge on a Needs attention row. Both change only the signed-in user's
 *  notifications: the row stays on the page, marked, and the item itself never changes. Acknowledge is for renewal
 *  actions only and holds until the next reminder threshold. */

/** State that belongs to one opening of a dialog (its error, its field errors): back to `initial` whenever `key` changes
 *  (the dialog opens or closes, or its target changes), so a refusal for one row never shows in the dialog for another,
 *  and a result that arrives after the key moved on is dropped. Pass `open ? target id : null` as the key. */
export function useResetOn<T>(key: unknown, initial: T) {
  const [state, setState] = useState<{ key: unknown; value: T }>({ key, value: initial });
  if (state.key !== key) setState({ key, value: initial }); // adjusting state while rendering, before anything shows
  const value = state.key === key ? state.value : initial;
  return [value, (next: T) => setState({ key, value: next })] as const;
}

export type QuietTarget = { queue: AttentionQueue; subjectId: string; name: string; snooze: Snooze | null };

export function SnoozeMark({ snooze }: { snooze: Snooze | null }) {
  if (!snooze) return null;
  return (
    <span className={chip({ state: "neutral" })}>
      <span aria-hidden="true">z</span>{snooze.kind === "snooze" ? `Snoozed until ${snooze.until}` : "Acknowledged"}
    </span>
  );
}

/** The row's Notifications menu. `onDone` gets the notice text and how to undo it. */
export function QuietMenu({ target, onDone }: { target: QuietTarget; onDone: (text: string, undo?: () => Promise<void>) => void }) {
  const [snoozing, setSnoozing] = useState(false);
  const [error, setError] = useState("");
  async function acknowledge() {
    try {
      const made = await api<Snooze>("needs-attention/snoozes", { method: "POST", body: { subject_type: target.queue, subject_id: target.subjectId, kind: "acknowledge" } });
      onDone(`${target.name} acknowledged. It's left out of your digest until its next reminder; it stays here.`, () => api(`needs-attention/snoozes/${made.id}`, { method: "DELETE", headers: ANY_REVISION }));
    } catch (e) {
      setError(e instanceof Problem ? e.detail : "Couldn't acknowledge it.");
    }
  }
  async function unsnooze() {
    if (!target.snooze) return;
    try {
      // Your own snooze, ended in one click: nothing read earlier is sent back, so any revision.
      await api(`needs-attention/snoozes/${target.snooze.id}`, { method: "DELETE", headers: ANY_REVISION });
      onDone(`Notifications for ${target.name} are back on.`);
    } catch (e) {
      setError(e instanceof Problem ? e.detail : "Couldn't remove the snooze.");
    }
  }
  const actions = target.snooze
    ? [{ label: target.snooze.kind === "snooze" ? "End snooze" : "Undo acknowledgment", onSelect: () => void unsnooze() }]
    : [
        { label: "Snooze notifications…", onSelect: () => setSnoozing(true) },
        ...(target.queue === "renewal_actions" ? [{ label: "Acknowledge", onSelect: () => void acknowledge() }] : []),
      ];
  return (
    <>
      <MoreMenu actions={actions} label="Notifications" about={target.name} />
      {error && <span className="sr-only" role="alert">{error}</span>}
      <SnoozeDialog open={snoozing} target={target} onClose={() => setSnoozing(false)}
        onDone={(text, undo) => { setSnoozing(false); onDone(text, undo); }} />
    </>
  );
}

function SnoozeDialog({ open, target, onClose, onDone }: {
  open: boolean; target: QuietTarget; onClose: () => void; onDone: (text: string, undo: () => Promise<void>) => void;
}) {
  const returnFocus = useReturnFocus(open);
  const timeZone = useMe().organization.timezone;
  const plusDays = (n: number) => orgToday(timeZone, n);
  const [until, setUntil] = useState(plusDays(7));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const made = await api<Snooze>("needs-attention/snoozes", { method: "POST", body: { subject_type: target.queue, subject_id: target.subjectId, kind: "snooze", until } });
      setErrors({});
      onDone(`Notifications for ${target.name} snoozed until ${until}. It stays in Needs attention, marked snoozed.`,
        () => api(`needs-attention/snoozes/${made.id}`, { method: "DELETE", headers: ANY_REVISION }));
    } catch (err) {
      setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { snooze_until: [err instanceof Problem ? err.detail : "Something went wrong."] });
    } finally {
      setSaving(false);
    }
  }
  // The API names the field `until`; on this page the date input is `snooze_until`.
  const shown: Record<string, string[]> = Object.fromEntries(Object.entries(errors).map(([k, v]) => [k === "until" ? "snooze_until" : k, v]));
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Snooze notifications for {target.name}?</DialogTitle>
          <DialogDescription>Leaves it out of your daily digest until the date. It stays in Needs attention and its deadline doesn't change.</DialogDescription>
        </DialogHeader>
        {open && (
          <form onSubmit={save} noValidate>
            <ErrorSummary errors={shown} />
            <Field id="snooze_until" label="Snooze until" errors={shown} help="Up to 90 days ahead.">
              <Input id="snooze_until" type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
            </Field>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
              <Button type="submit" disabled={saving}>{saving ? "Snoozing…" : "Snooze"}</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---- Reminders: snooze, acknowledge, reopen and resolve -------------------------------------------------

/** A reminder's status as of today, as a chip with a glyph and words. */
export function ReminderStatusChip({ status, until }: { status: ReminderStatus; until: string | null }) {
  if (status === "snoozed") return <span className={chip({ state: "neutral" })}><span aria-hidden="true">z</span>Snoozed until {until}</span>;
  if (status === "acknowledged") return <span className={chip({ state: "neutral" })}><span aria-hidden="true">✓</span>Acknowledged</span>;
  if (status === "open") return <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Open</span>;
  return <span className={chip({ state: "neutral" })}><span aria-hidden="true">–</span>{status === "done" ? "Done" : "No longer applies"}</span>;
}

/** Reminder sources whose manual resolution needs a note. Mirrors notify/services.resolve_reminder, which enforces it:
 *  change both together (this only marks the field "(required)"). */
const NOTE_REQUIRED: ReminderSourceType[] = ["renewal_cycle", "contract", "periodic_review", "credential_expiry", "vendor_risk", "end_of_life"];
export type ReminderTarget = { id: string; name: string; status: ReminderStatus; sourceType: ReminderSourceType };
/** A 412 from a reminder dialog: the reminder changed after the dialog opened. */
const STALE_REMINDER = "Someone else changed this reminder after you opened this, so nothing changed. Close this and check it, then try again.";
const reminderError = (e: unknown, what: string) => (e instanceof Problem ? e.detail : `Couldn't ${what}. Try again.`);

/** The row's Reminder menu (Editors and Admins). Every action changes the reminder for everyone it reaches, and
 *  the row stays in Needs attention until it is resolved. `onDone` gets the notice and whether the row leaves. */
export function ReminderActions({ target, onDone }: { target: ReminderTarget; onDone: (text: string, gone: boolean) => void }) {
  const [dialog, setDialog] = useState<"snooze" | "resolve" | null>(null);
  const [error, setError] = useState("");
  async function act(action: "acknowledge" | "reopen") {
    setError("");
    try {
      // If-Match. Acknowledge names its whole change: any revision. Reopen clears a resolution, so it is sent
      // with the reminder's ETag read now, and only while the reminder is still in the state this row shows; otherwise
      // someone else changed it (resolved it, say) and nothing is reopened.
      let headers = ANY_REVISION;
      if (action === "reopen") {
        const now = await apiWithEtag<{ status: ReminderStatus }>(`reminders/${target.id}`);
        if (now.data.status !== target.status) return setError(STALE_REMINDER);
        headers = ifMatch(now.etag) ?? ANY_REVISION;
      }
      await api(`reminders/${target.id}/${action}`, { method: "POST", body: {}, headers });
      onDone(action === "acknowledge"
        ? `${target.name} acknowledged: its advance notices stop. It stays here until it is resolved.`
        : `${target.name} reopened: its notices go out again.`, false);
    } catch (e) {
      setError(isStaleWrite(e) ? STALE_REMINDER : reminderError(e, action === "acknowledge" ? "acknowledge it" : "reopen it"));
    }
  }
  const actions = [
    { label: "Snooze…", onSelect: () => setDialog("snooze") },
    ...(target.status !== "acknowledged" ? [{ label: "Acknowledge", onSelect: () => void act("acknowledge") }] : []),
    ...(target.status !== "open" ? [{ label: "Reopen", onSelect: () => void act("reopen") }] : []),
    { label: "Resolve…", onSelect: () => setDialog("resolve") },
  ];
  return (
    <>
      <MoreMenu actions={actions} label="Reminder" about={target.name} size="sm" />
      {error && <span className="text-sm text-error-foreground" role="alert">{error}</span>}
      <ReminderSnoozeDialog open={dialog === "snooze"} target={target} onClose={() => setDialog(null)}
        onDone={(text) => { setDialog(null); onDone(text, false); }} />
      <ReminderResolveDialog open={dialog === "resolve"} target={target} onClose={() => setDialog(null)}
        onDone={(text) => { setDialog(null); onDone(text, true); }} />
    </>
  );
}

/** Field errors from the API (`until`, `kind`, `note`, `evidence`) under this dialog's element IDs. */
const prefixed = (errors: Record<string, string[]>) => Object.fromEntries(Object.entries(errors).map(([k, v]) => [`reminder_${k}`, v]));

function ReminderSnoozeDialog({ open, target, onClose, onDone }: { open: boolean; target: ReminderTarget; onClose: () => void; onDone: (text: string) => void }) {
  const returnFocus = useReturnFocus(open);
  const timeZone = useMe().organization.timezone;
  const [until, setUntil] = useState(orgToday(timeZone, 7));
  const [errors, setErrors] = useResetOn<Record<string, string[]>>(open, {});
  const [saving, setSaving] = useState(false);
  // Read with its ETag as the dialog opens; the snooze sends it as If-Match.
  const opened = useOpenedRecord(open ? `reminders/${target.id}` : null);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      await api(`reminders/${target.id}/snooze`, { method: "POST", body: { until }, headers: ifMatch(await opened.etag()) });
      setErrors({});
      onDone(`${target.name} snoozed until ${until}: no notices for anyone until then. It stays in Needs attention, marked snoozed.`);
    } catch (err) {
      setErrors(err instanceof Problem && Object.keys(err.errors).length ? prefixed(err.errors) : { reminder_until: [isStaleWrite(err) ? STALE_REMINDER : reminderError(err, "snooze it")] });
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Snooze the reminder for {target.name}?</DialogTitle>
          <DialogDescription>No notices go out for it, to anyone, through the date. The day after, it is open again. The deadline doesn't change.</DialogDescription>
        </DialogHeader>
        {open && (
          <form onSubmit={save} noValidate>
            <ErrorSummary errors={errors} />
            <Field id="reminder_until" label="Snooze until" errors={errors} help="After today, up to 90 days ahead.">
              <Input id="reminder_until" type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
            </Field>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
              <Button type="submit" disabled={saving}>{saving ? "Snoozing…" : "Snooze reminder"}</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

const RESOLUTIONS = [
  { value: "done", label: "Done", help: "The work is done." },
  { value: "risk_accepted", label: "Risk accepted", help: "It won't be done; say why in the note." },
  { value: "no_longer_applies", label: "No longer applies", help: "The obligation went away." },
] as const;

function ReminderResolveDialog({ open, target, onClose, onDone }: { open: boolean; target: ReminderTarget; onClose: () => void; onDone: (text: string) => void }) {
  const returnFocus = useReturnFocus(open);
  const [kind, setKind] = useState<(typeof RESOLUTIONS)[number]["value"]>("done");
  const [note, setNote] = useState("");
  const [evidence, setEvidence] = useState("");
  const [errors, setErrors] = useResetOn<Record<string, string[]>>(open, {});
  const [saving, setSaving] = useState(false);
  const noteRequired = kind === "risk_accepted" || NOTE_REQUIRED.includes(target.sourceType);
  // Read with its ETag as the dialog opens; the resolution sends it as If-Match.
  const opened = useOpenedRecord(open ? `reminders/${target.id}` : null);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const body: Record<string, string> = { kind };
      if (note.trim()) body.note = note.trim();
      if (evidence.trim()) body.evidence = evidence.trim();
      await api(`reminders/${target.id}/resolve`, { method: "POST", body, headers: ifMatch(await opened.etag()) });
      setErrors({});
      onDone(`${target.name} resolved (${RESOLUTIONS.find((r) => r.value === kind)!.label}). Its notices stop and it leaves Needs attention.`);
    } catch (err) {
      setErrors(err instanceof Problem && Object.keys(err.errors).length ? prefixed(err.errors) : { reminder_kind: [isStaleWrite(err) ? STALE_REMINDER : reminderError(err, "resolve it")] });
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Resolve the reminder for {target.name}?</DialogTitle>
          <DialogDescription>Closes the reminder for everyone. It doesn't change the source: a renewal still needs its decision recorded.</DialogDescription>
        </DialogHeader>
        {open && (
          <form onSubmit={save} noValidate>
            <ErrorSummary errors={errors} />
            <fieldset className="mb-3" aria-describedby={errors.reminder_kind ? "reminder_kind-error" : undefined}>
              <legend className="mb-1.5 text-sm font-medium">How was it resolved?</legend>
              {RESOLUTIONS.map((r, i) => (
                <label key={r.value} className="flex min-h-8 items-start gap-2 py-1 text-sm">
                  <input type="radio" name="reminder_kind" id={i === 0 ? "reminder_kind" : undefined} value={r.value} checked={kind === r.value}
                    onChange={() => setKind(r.value)} className="mt-0.5 h-4 w-4 accent-primary" />
                  <span><span className="font-medium">{r.label}</span><span className="block text-muted-foreground">{r.help}</span></span>
                </label>
              ))}
              {errors.reminder_kind && <span className="text-sm text-error-foreground" id="reminder_kind-error">{errors.reminder_kind.join(" ")}</span>}
            </fieldset>
            <Field id="reminder_note" label={noteRequired ? "Note (required)" : "Note"} errors={errors}>
              <Textarea id="reminder_note" value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
            <Field id="reminder_evidence" label="Evidence" errors={errors} help="A reference such as a ticket or a document.">
              <Input id="reminder_evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} maxLength={500} />
            </Field>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
              <Button type="submit" disabled={saving}>{saving ? "Resolving…" : "Resolve reminder"}</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
