/** Archive and delete confirmations (Restore has no dialog). They only ask and report; the parent owns the request.
 *
 *  `onConfirm` runs the request and resolves when it succeeds (the parent then closes the dialog). `pending` is true
 *  while it runs. `refusal` is the 409 problem `detail`, shown inline, for example "X can't be deleted because it has 3 reviews and 1 instance. Archive it instead."
 *  Focus returns to whatever had it when the dialog opened, so open it from a control that stays mounted.
 */
import { useState } from "react";
import { useReturnFocus } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export type LifecycleDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Record name, used in the title and the confirm button. */
  name: string;
  onConfirm: () => Promise<void>;
  pending?: boolean;
  refusal?: string;
};

function Refusal({ text }: { text?: string }) {
  if (!text) return null;
  return <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{text}</p>;
}

const wrapLabel = "h-auto min-h-9 max-w-full whitespace-normal py-1.5 text-left [overflow-wrap:anywhere]";

function Shell({ open, onOpenChange, title, description, children }: {
  open: boolean; onOpenChange: (o: boolean) => void; title: string; description: string; children: React.ReactNode;
}) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {open && children}
      </DialogContent>
    </Dialog>
  );
}

export function ArchiveDialog({ open, onOpenChange, name, onConfirm, pending, refusal }: LifecycleDialogProps) {
  return (
    <Shell open={open} onOpenChange={onOpenChange} title={`Archive ${name}?`}
      description="It disappears from day-to-day lists and Needs attention. History, reviews and links are kept. You can restore it any time.">
      <Refusal text={refusal} />
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button type="button" className={wrapLabel} disabled={pending} onClick={() => void onConfirm()}>{pending ? "Archiving…" : "Archive"}</Button>
      </DialogFooter>
    </Shell>
  );
}

export function DeleteDialog({ open, onOpenChange, name, onConfirm, pending, refusal, onArchiveInstead }: LifecycleDialogProps & {
  /** Offered beside the refusal, because a record with history can be archived instead. */
  onArchiveInstead?: () => void;
}) {
  return (
    <Shell open={open} onOpenChange={onOpenChange} title={`Delete ${name}?`}
      description="This can't be undone. Type the name to confirm.">
      <DeleteForm name={name} onCancel={() => onOpenChange(false)} onConfirm={onConfirm} pending={pending} refusal={refusal} onArchiveInstead={onArchiveInstead} />
    </Shell>
  );
}

function DeleteForm({ name, onCancel, onConfirm, pending, refusal, onArchiveInstead }: {
  name: string; onCancel: () => void; onConfirm: () => Promise<void>; pending?: boolean; refusal?: string; onArchiveInstead?: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [mismatch, setMismatch] = useState(false);
  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (typed.trim() !== name) return setMismatch(true);
    setMismatch(false);
    void onConfirm();
  }
  return (
    <form onSubmit={submit} noValidate className="grid gap-4">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="delete-confirm-name">Name (required)</Label>
        <Input id="delete-confirm-name" value={typed} autoComplete="off" aria-invalid={mismatch || undefined}
          aria-describedby={mismatch ? "delete-confirm-error" : undefined} onChange={(e) => setTyped(e.target.value)} />
        {mismatch && <span id="delete-confirm-error" className="text-sm text-error-foreground">Type the name exactly as shown in the title.</span>}
      </div>
      <Refusal text={refusal} />
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        {refusal && onArchiveInstead && <Button type="button" variant="outline" onClick={onArchiveInstead}>Archive instead</Button>}
        <Button type="submit" variant="destructive" className={wrapLabel} disabled={pending}>{pending ? "Deleting…" : `Delete ${name}`}</Button>
      </DialogFooter>
    </form>
  );
}
