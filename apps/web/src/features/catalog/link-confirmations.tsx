import { useState } from "react";
import { api, LinkConfirmation, LinkState, orgToday, Person } from "@/api";
import { useMe } from "@/lib/session";
import { attempt, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, Select, TableRegion, useLoad, useReturnFocus } from "@/components/app";
import { DetailList, DetailRow, LinkStateChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

/** Confirm link (same pattern as Record review) and confirmation history, for relationships and integrations. */

export type ConfirmTarget = {
  kind: "relationship" | "integration";
  id: string;
  /** The link in words, for example "Payroll Sync depends on Workday". */
  sentence: string;
  state: LinkState;
  why: string;
};

const collection = (kind: ConfirmTarget["kind"]) => (kind === "relationship" ? "relationships" : "integrations");

/** Open it from a control that stays mounted; focus returns to it when the dialog closes. */
export function ConfirmLinkDialog({ target, onClose, onDone }: { target: ConfirmTarget | null; onClose: () => void; onDone: (message: string) => void }) {
  const returnFocus = useReturnFocus(target !== null);
  return (
    <Dialog open={target !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Confirm link</DialogTitle>
          <DialogDescription>You're recording that someone confirmed this link is still accurate. Owners don't sign in; record how you got the confirmation.</DialogDescription>
        </DialogHeader>
        {target && <ConfirmForm target={target} onCancel={onClose} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

function ConfirmForm({ target, onCancel, onDone }: { target: ConfirmTarget; onCancel: () => void; onDone: (m: string) => void }) {
  const people = useLoad<Person[]>("people");
  const today = orgToday(useMe().organization.timezone);
  const [form, setForm] = useState({ confirmed_by: "", confirmed_on: today, evidence: "", inactive_reason: "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const chosen = people.data?.find((p) => p.id === form.confirmed_by);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const failed = await attempt(() => api(`${collection(target.kind)}/${target.id}/confirmations`, { method: "POST", body: form }));
    setBusy(false);
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { evidence: [failed.detail] });
    onDone("Link confirmed.");
  }
  const sorted = [...(people.data ?? [])].sort((x, y) => Number(y.active) - Number(x.active));
  if (people.error) return <ErrorBanner message={people.error} onRetry={people.reload} />;
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <DetailList className="mb-4 rounded-md border bg-table-head [&>dd]:py-1.5 [&>dt]:py-1.5">
        <DetailRow label="Link"><span className="[overflow-wrap:anywhere]">{target.sentence}</span></DetailRow>
        <DetailRow label="Link state"><span className="flex flex-wrap items-center gap-2"><LinkStateChip state={target.state} /><span className="text-sm text-muted-foreground">{target.why}</span></span></DetailRow>
      </DetailList>
      <Field id="confirmed_by" label="Confirmed by (required)" errors={errors}>
        <Select id="confirmed_by" value={form.confirmed_by} onChange={set("confirmed_by")}>
          <option value="">Choose a person</option>
          {sorted.map((p) => <option key={p.id} value={p.id}>{p.name}{p.active ? "" : " (inactive)"}</option>)}
        </Select>
      </Field>
      {chosen && !chosen.active && (
        <Field id="inactive_reason" label="Reason for using an inactive person (required)" errors={errors}>
          <Input id="inactive_reason" value={form.inactive_reason} onChange={set("inactive_reason")} />
        </Field>
      )}
      <Field id="confirmed_on" label="Confirmation date (required)" errors={errors} help="The link needs verifying again after the organization's review interval, or when it changes.">
        <Input id="confirmed_on" type="date" value={form.confirmed_on} max={today} onChange={set("confirmed_on")} />
      </Field>
      <Field id="evidence" label="Evidence / reference (required)" errors={errors}>
        <Input id="evidence" value={form.evidence} onChange={set("evidence")} placeholder="For example: email from Dana, 31 Oct" />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : "Confirm link"}</Button>
      </DialogFooter>
    </form>
  );
}

export function ConfirmationTable({ rows, label }: { rows: LinkConfirmation[]; label: string }) {
  return (
    <TableRegion label={`${label} table`}>
      <Table>
        <TableCaption className="sr-only">{label}, newest first. Confirmations can't be edited.</TableCaption>
        <TableHeader><TableRow>
          <TableHead aria-sort="descending">Confirmed</TableHead><TableHead>Confirmed by</TableHead><TableHead>Recorded by</TableHead><TableHead>Evidence / reference</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {rows.map((v) => (
            <TableRow key={v.id} className="h-[52px]">
              <TableHead scope="row" className="tabular font-semibold text-foreground"><time dateTime={v.confirmed_on}>{v.confirmed_on}</time></TableHead>
              <TableCell>{v.confirmed_by.name}{!v.confirmed_by.active && <span className="text-muted-foreground"> (inactive)</span>}</TableCell>
              <TableCell>{v.recorded_by.name}</TableCell>
              <TableCell className="max-w-sm whitespace-normal [overflow-wrap:anywhere]">{v.evidence}{v.inactive_reason && <span className="text-muted-foreground"> · Reason: {v.inactive_reason}</span>}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

/** The history states shared by the dialog and the integration tab. */
export function ConfirmationHistory({ load }: { load: ReturnType<typeof useLoad<LinkConfirmation[]>> }) {
  if (load.error) return <ErrorBanner message={load.error} onRetry={load.reload} />;
  if (!load.data) return <Loading what="confirmation history" rows={3} />;
  if (load.data.length === 0) return <EmptyState title="Never confirmed">Confirm the link once someone has checked, through your usual channel, that it is still accurate.</EmptyState>;
  return <ConfirmationTable rows={load.data} label="Confirmation history" />;
}

/** History for one relationship row, in a dialog (any role can read it). */
export function HistoryDialog({ target, onClose }: { target: Pick<ConfirmTarget, "id" | "sentence"> | null; onClose: () => void }) {
  const returnFocus = useReturnFocus(target !== null);
  return (
    <Dialog open={target !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Confirmation history</DialogTitle>
          <DialogDescription className="[overflow-wrap:anywhere]">{target?.sentence}</DialogDescription>
        </DialogHeader>
        {target && <HistoryBody id={target.id} />}
      </DialogContent>
    </Dialog>
  );
}

function HistoryBody({ id }: { id: string }) {
  const load = useLoad<LinkConfirmation[]>(`relationships/${id}/confirmations`);
  return <ConfirmationHistory load={load} />;
}
