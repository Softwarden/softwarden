/** Add or change a data-quality exception: a reasoned "not applicable" for one record and one rule, with
 *  an accountable active person and an expiry from today to a year ahead (a year by default). On a change the record
 *  and the rule are fixed, the expiry can only move later (extend-only, as the API enforces) and the save sends
 *  If-Match; an exception whose owner has left must be given an active owner before it can be extended. Loaded on first
 *  use (record pages, the data quality page). */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiWithEtag, orgToday, Problem, type Person, type QualityException } from "@/api";
import { ErrorBanner, ErrorSummary, Field, Loading, Select, Textarea, useReturnFocus } from "@/components/app";
import { Combobox, type ComboOption } from "@/components/combobox";
import { RecordPicker, type PickedRecord } from "@/components/record-picker";
import { GapChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { apiKey, problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import { KIND_LABEL, type RecordKind } from "@/lib/kinds";
import { useQualityRules } from "./completeness";

const REASON_MAX = 1000;
const DAYS_MAX = 365;
/** API keys to this form's field IDs (unique on the page, so the error summary's links and first-field focus work). */
const IDS: Record<string, string> = { record: "exception-record", rule: "exception-rule", reason: "exception-reason", owner: "exception-owner", expires_on: "exception-expires" };
const shown = (errors: Record<string, string[]>) => Object.fromEntries(Object.entries(errors).map(([k, v]) => [IDS[k] ?? k, v]));

type RecordRef = { id: string; kind: RecordKind; name: string };

export function ExceptionDialog({ open, onClose, onDone, exception, record, rule }: {
  open: boolean; onClose: () => void; onDone: (text: string) => void;
  /** Changing an existing exception. */
  exception?: QualityException | null;
  /** Adding: the record and rule chosen already (from a record page). */
  record?: RecordRef; rule?: string;
}) {
  const returnFocus = useReturnFocus(open);
  const editing = !!exception;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{editing ? `Change the exception — ${exception!.record.name}` : record ? `Add an exception — ${record.name}` : "Add an exception"}</DialogTitle>
          <DialogDescription>
            {editing ? "The record and the check stay as they are. The expiry can only move later; remove the exception to end it sooner."
              : "Marks one check not applicable to one record, with a reason and someone who answers for it. The check counts again when it expires."}
          </DialogDescription>
        </DialogHeader>
        {open && (editing ? <EditLoader exception={exception!} onClose={onClose} onDone={onDone} /> : <ExceptionForm record={record} rule={rule} onClose={onClose} onDone={onDone} etag={null} />)}
      </DialogContent>
    </Dialog>
  );
}

function EditLoader({ exception, onClose, onDone }: { exception: QualityException; onClose: () => void; onDone: (t: string) => void }) {
  const path = `data-quality/exceptions/${exception.id}`;
  const fresh = useQuery({ queryKey: [...apiKey(path), "etag"], queryFn: () => apiWithEtag<QualityException>(path), gcTime: 0, staleTime: Infinity, retry: false });
  const [round, setRound] = useState(0);
  const [stale, setStale] = useState(false);
  if (fresh.error) return <ErrorBanner message={problemText(fresh.error, "Couldn't load the exception.")} onRetry={() => void fresh.refetch()} />;
  if (!fresh.data) return <Loading what="the exception" />;
  async function loadLatest() {
    await fresh.refetch();
    setStale(false);
    setRound((n) => n + 1);
  }
  return (
    <>
      {stale && (
        <div className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
          <p className="font-semibold">Someone else changed this exception after the dialog opened, so your change wasn't saved.</p>
          <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => void loadLatest()}>Load the latest version</Button>
        </div>
      )}
      <ExceptionForm key={round} existing={fresh.data.data} etag={fresh.data.etag} onClose={onClose} onDone={onDone} onStale={() => setStale(true)} />
    </>
  );
}

function ExceptionForm({ existing, etag, record: preset, rule: presetRule, onClose, onDone, onStale }: {
  existing?: QualityException; etag: string | null; record?: RecordRef; rule?: string;
  onClose: () => void; onDone: (t: string) => void; onStale?: () => void;
}) {
  const me = useMe();
  const client = useQueryClient();
  const today = orgToday(me.organization.timezone);
  const latest = orgToday(me.organization.timezone, DAYS_MAX);
  const rules = useQualityRules();
  const people = useApiQuery<Person[]>("people?active=true", { staleTime: 30_000 });
  const fixedRecord: RecordRef | null = existing ? existing.record : preset ?? null;
  const [picked, setPicked] = useState<PickedRecord | null>(null);
  const record = fixedRecord ?? (picked ? { id: picked.id, kind: picked.kind as RecordKind, name: picked.name } : null);
  const [rule, setRule] = useState(existing?.rule ?? presetRule ?? "");
  const [reason, setReason] = useState(existing?.reason ?? "");
  const [owner, setOwner] = useState(existing?.owner.id ?? "");
  const [expires, setExpires] = useState(existing?.expires_on ?? latest);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState<{ text: string; existingFor?: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const ownerLeft = !!existing && !existing.owner.active;

  // Rules a record may be excepted from: exceptable, on here, and checking the record's kind.
  const recordKind = record?.kind ?? null;
  const choices = useMemo(() => (rules.data ?? []).filter((r) => r.exceptable && r.status === "on" && (!recordKind || r.kinds.includes(recordKind))), [rules.data, recordKind]);
  const kinds: RecordKind[] = me.capabilities.includes("ai_agents") ? ["application", "component", "ai_agent"] : ["application", "component"];
  const ownerOptions: ComboOption[] = [
    ...(ownerLeft ? [{ id: existing!.owner.id, label: existing!.owner.name, hint: "has left: choose an active person to extend" }] : []),
    ...(people.data ?? []).map((p) => ({ id: p.id, label: p.name, hint: [p.department?.name, p.email].filter(Boolean).join(" · ") })),
  ];

  // Extend-only: an expired exception can be renewed from today, a current one only moved later.
  const earliest = existing && existing.expires_on > today ? existing.expires_on : today;

  function check(): Record<string, string[]> {
    const e: Record<string, string[]> = {};
    if (!record) e.record = ["Choose a record."];
    if (!rule) e.rule = ["Choose a check."];
    if (!reason.trim()) e.reason = ["Say why this check doesn't apply."];
    else if (reason.trim().length > REASON_MAX) e.reason = [`Use ${REASON_MAX.toLocaleString()} characters or fewer.`];
    if (!owner) e.owner = ["Choose who answers for this decision."];
    // An unchanged expiry is never checked (changing only the reason of an expired exception is allowed).
    const moved = !existing || expires !== existing.expires_on;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(expires)) e.expires_on = ["Enter a date as YYYY-MM-DD."];
    else if (!moved) { /* unchanged */ }
    else if (expires < earliest) {
      e.expires_on = [existing && earliest === existing.expires_on
        ? `Choose ${earliest} or later; remove the exception to end it sooner.`
        : "Choose today or a later date."];
    }
    else if (expires > latest) e.expires_on = [`Choose a date at most ${DAYS_MAX} days ahead (${latest}).`];
    if (existing && ownerLeft && owner === existing.owner.id && expires !== existing.expires_on) e.owner = ["Choose an active person to extend it."];
    return e;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBanner(null);
    const found = check();
    setErrors(found);
    if (Object.keys(found).length) return;
    setSaving(true);
    try {
      if (existing) {
        const body: Record<string, string> = {};
        if (reason.trim() !== existing.reason) body.reason = reason.trim();
        if (owner !== existing.owner.id) body.owner = owner;
        if (expires !== existing.expires_on) body.expires_on = expires;
        if (!Object.keys(body).length) return onClose();
        await api(`data-quality/exceptions/${existing.id}`, { method: "PATCH", body, headers: etag ? { "If-Match": etag } : undefined });
      } else {
        await api("data-quality/exceptions", { method: "POST", body: { record: record!.id, rule, reason: reason.trim(), owner, expires_on: expires } });
      }
      await Promise.all(["data-quality", "needs-attention", "applications", "components", "ai-agents"].map((p) => client.invalidateQueries({ queryKey: apiKey(p) })));
      const label = (rules.data ?? []).find((r) => r.id === rule)?.label ?? rule;
      onDone(existing ? `Exception for “${label}” on ${record!.name} saved, until ${expires}.` : `“${label}” marked not applicable to ${record!.name} until ${expires}.`);
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && onStale) return onStale();
      if (err instanceof Problem && err.status === 409) return setBanner({ text: err.detail, existingFor: record?.id });
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setBanner({ text: `${problemText(err)} Nothing was saved.` });
    } finally {
      setSaving(false);
    }
  }

  if (rules.isError) return <ErrorBanner message={problemText(rules.error, "Couldn't load the data-quality checks.")} onRetry={() => void rules.refetch()} />;
  if (!rules.data || !people.data) return <Loading what="the form" />;
  const errs = shown(errors);
  const ruleLabel = (id: string) => rules.data!.find((r) => r.id === id)?.label ?? id;
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errs} focusOnShow="field" />
      {banner && (
        <div className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
          <p className="font-semibold">{banner.text}</p>
          {banner.existingFor && <Link to={`/data-quality?tab=exceptions&record=${banner.existingFor}`} onClick={onClose}>Open the record's exceptions</Link>}
        </div>
      )}
      {fixedRecord ? (
        <p className="mb-3 text-sm"><span className="text-muted-foreground">Record: </span><span className="font-semibold">{fixedRecord.name}</span> <span className="text-muted-foreground">({KIND_LABEL[fixedRecord.kind]})</span></p>
      ) : (
        <Field id={IDS.record} label="Record (required)" errors={errs}>
          <RecordPicker id={IDS.record} value={picked} onChange={(r) => { setPicked(r); if (r && rule && !choices.some((c) => c.id === rule && c.kinds.includes(r.kind as RecordKind))) setRule(""); }}
            kinds={kinds} placeholder="Search records…" listLabel="Record suggestions" />
        </Field>
      )}
      {existing ? (
        <p className="mb-3 text-sm"><span className="text-muted-foreground">Check: </span><span className="font-semibold">{existing.rule_label}</span></p>
      ) : (
        <Field id={IDS.rule} label="Check (required)" errors={errs} help="Only checks that are on and may be marked not applicable.">
          <Select id={IDS.rule} value={rule} onChange={(e) => setRule(e.target.value)}>
            <option value="">Choose a check</option>
            {choices.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
            {rule && !choices.some((r) => r.id === rule) && <option value={rule}>{ruleLabel(rule)}</option>}
          </Select>
        </Field>
      )}
      <Field id={IDS.reason} label="Reason (required)" errors={errs} help="Why the check doesn't apply, for whoever reads this next.">
        <Textarea id={IDS.reason} value={reason} maxLength={REASON_MAX} onChange={(e) => setReason(e.target.value)} />
      </Field>
      {ownerLeft && <p className="mb-2 flex flex-wrap items-center gap-2 text-sm"><GapChip label="Owner left" /><span>{existing!.owner.name} is no longer active. Choose an active person to extend this exception.</span></p>}
      <Field id={IDS.owner} label="Accountable person (required)" errors={errs} help="An active person who answers for this decision.">
        <Combobox id={IDS.owner} options={ownerOptions} value={owner} onChange={setOwner} placeholder="Search people…" listLabel="Accountable person suggestions" />
      </Field>
      <Field id={IDS.expires_on} label="Expires on (required)" errors={errs}
        help={existing ? (existing.active ? `Extend only: ${existing.expires_on} to ${latest}.` : `It expired on ${existing.expires_on}; renew it from today to ${latest}.`) : `The last day it applies: today to ${latest} (a year ahead, the default).`}>
        <Input id={IDS.expires_on} type="date" min={earliest} max={latest} value={expires} onChange={(e) => setExpires(e.target.value)} className="w-48" />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : existing ? "Save exception" : "Add exception"}</Button>
      </DialogFooter>
    </form>
  );
}
