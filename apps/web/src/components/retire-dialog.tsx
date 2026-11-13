/** Retire with acknowledgment. Shown when the API answers 409
 *  `/problems/retirement-needs-acknowledgment`: it names the active dependents, says which records don't need
 *  acknowledging, and keeps the button off until there is a reason and the box is ticked. Used by the record edit form
 *  (lifecycle to Retired) and by the assessment status menu (a retirement assessment to Ready for external execution).
 *
 *  `RetireAckForm` is the body, so the status dialog can swap to it in place; `RetireDialog` wraps it for the edit form. */
import { useEffect, useState } from "react";
import { cap, RetirementCheck } from "@/api";
import { KIND_LABEL } from "@/lib/kinds";
import { Textarea } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";

export type RetireMode = "record" | "assessment";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export function retireCopy(mode: RetireMode, name: string, check: RetirementCheck) {
  const n = check.dependents.length;
  return {
    title: mode === "record" ? `Retire ${name}?` : `Mark the retirement of ${name} ready?`,
    description: mode === "record"
      ? `${plural(n, "active record still relies", "active records still rely")} on ${name}. Retiring it doesn't remove any dependency evidence.`
      : `${name} still has active dependents. Moving this assessment to Ready for external execution needs an acknowledgment. It doesn't retire ${name} or remove any link.`,
    submit: mode === "record" ? "Retire with acknowledgment" : "Acknowledge and mark ready",
    reasonLabel: mode === "record" ? "Reason (required, saved to the audit history)" : "Reason (required, saved with this assessment and the audit history)",
  };
}

/** The body: dependents, who doesn't need acknowledging, the reason, the checkbox and the footer. `onConfirm` resolves to an error
 *  message to keep the dialog open, or null once the request went through (the parent then closes it). */
export function RetireAckForm({ mode, name, check, onCancel, onConfirm }: {
  mode: RetireMode; name: string; check: RetirementCheck; onCancel: () => void; onConfirm: (reason: string) => Promise<string | null>;
}) {
  const [reason, setReason] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { document.getElementById("retirement_ack_reason")?.focus(); }, []);
  const copy = retireCopy(mode, name, check);
  const n = check.dependents.length;
  const ready = reason.trim() !== "" && ack;
  const missing = !reason.trim() && !ack ? "Enter a reason and tick the box to continue." : !reason.trim() ? "Enter a reason to continue." : !ack ? "Tick the box to continue." : "";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError("");
    const failed = await onConfirm(reason.trim());
    setBusy(false);
    if (failed) setError(failed);
  }
  const notRequired = check.not_required;
  return (
    <form onSubmit={submit} noValidate className="grid gap-4">
      <div>
        <p className="font-semibold">{plural(n, "active dependent still relies", "active dependents still rely")} on {name}:</p>
        <ul className="ml-5 mt-1 list-disc" aria-label="Active dependents">
          {check.dependents.map((d) => <li key={d.id}>{d.name} <span className="text-muted-foreground">({KIND_LABEL[d.kind] ?? cap(d.kind)}, {cap(d.lifecycle)})</span></li>)}
        </ul>
        {notRequired.length > 0 && (
          <p className="mt-2 text-sm text-muted-foreground">
            {notRequired.length === 1
              ? `${notRequired[0].name} is ${cap(notRequired[0].reason)} and doesn't need acknowledgment.`
              : `${notRequired.map((r) => `${r.name} (${cap(r.reason)})`).join(", ")} don't need acknowledgment.`}
          </p>
        )}
        <p className="mt-2 text-sm text-muted-foreground">
          {mode === "record" ? "Retiring doesn't remove dependency evidence: every link is kept." : "Every link is kept. Nothing is retired until someone changes the record's lifecycle."}
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="retirement_ack_reason">{copy.reasonLabel}</Label>
        <Textarea id="retirement_ack_reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={5000}
          aria-describedby="retire-ack-hint" placeholder="For example: Kiosk moves to the new vendor API on 2026-12-01." />
      </div>
      <label className="flex items-start gap-2">
        <input type="checkbox" className="mt-1" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span>I acknowledge {plural(n, "active dependent", "active dependents")}</span>
      </label>
      {error && <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{error}</p>}
      <p id="retire-ack-hint" className="text-sm text-muted-foreground" role="status">{missing}</p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" aria-disabled={!ready || busy} aria-describedby="retire-ack-hint" className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50">
          {busy ? "Saving…" : copy.submit}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** Dialog for the record edit form. On cancel (or Escape) focus goes to `returnFocusId`, the Save button: the form
 *  disables it while saving, so it can't be remembered as the opener. */
export function RetireDialog({ open, onClose, returnFocusId, name, check, onConfirm }: {
  open: boolean; onClose: () => void; returnFocusId: string; name: string; check: RetirementCheck | null; onConfirm: (reason: string) => Promise<string | null>;
}) {
  const copy = check ? retireCopy("record", name, check) : null;
  return (
    <Dialog open={open && check !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" onCloseAutoFocus={(e) => { e.preventDefault(); document.getElementById(returnFocusId)?.focus(); }}>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">{copy?.title ?? "Retire?"}</DialogTitle>
          <DialogDescription>{copy?.description ?? ""}</DialogDescription>
        </DialogHeader>
        {check && <RetireAckForm mode="record" name={name} check={check} onCancel={onClose} onConfirm={onConfirm} />}
      </DialogContent>
    </Dialog>
  );
}
