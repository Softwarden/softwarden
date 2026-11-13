/** Administration › System status › Backups: the
 *  nightly backup the worker takes (core/backup.py), from `GET system`'s `backup` block and its `warnings`.
 *
 *  The state is one chip with a glyph and words: ✓ Backed up N hours ago, ! Stale, ⊘ Failed (with the stored stage
 *  message: the stage and a tool's exit status only, the rest is in the worker log), ○ No backup yet, ○ Not configured.
 *  Then the last good set (time, name, size, how long it took), the last failure, the destination, the schedule and the
 *  key. Each warning is listed with a fix hint matching `manage.py doctor` (core/doctor.py) and the backup runbook. The
 *  generated key's warning carries "I have copied the backup key somewhere safe…", confirmed in a dialog before
 *  `POST system/backup/acknowledge-key`; a 409 (the key is configured by file, or there is none yet) is said in words
 *  and the status reloaded. Backup warnings never change the overall status or /readyz. */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { HardDriveDownload } from "lucide-react";
import { Problem } from "@/api";
import { chip, DetailList, DetailRow, GapChip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Modal } from "@/features/admin/admin";
import { typedApi, type Schemas } from "@/lib/openapi";
import { apiKey, problemText, useInvalidate } from "@/lib/query";

type Backup = Schemas["BackupStatus"];
type Warning = Schemas["SystemWarning"];
type SystemStatus = Schemas["SystemStatus"];
type Tone = "confirmed" | "pending" | "overdue" | "unreviewed" | "neutral";
type Look = { tone: Tone; glyph: string; label: string };

/** Formatting helpers shared with the System page (system.tsx passes them in, so this file doesn't import it back). */
export type BackupFormat = {
  span: (seconds: number) => string;
  orgTime: (iso: string, timeZone: string) => string;
  cronInWords: (cron: string) => string | null;
};

// The Docker Compose commands the hints name, as core/doctor.py writes them (docs/INSTALL.md is the primary install).
const COMPOSE_WORKER = "docker compose exec worker python manage.py";
const KEY_STATE_ID = "backup-key-state";

/** A command or setting name in running text (shared with system.tsx). */
export const code = (text: string) => <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{text}</code>;

function Chip({ tone, glyph, label }: Look) {
  return <span className={chip({ state: tone })}><span aria-hidden="true">{glyph}</span>{label}</span>;
}

/** A failure recorded after the last good set (the API's own rule for `backup_failed`). */
export function failedSinceGood(b: Backup) {
  if (!b.last_failure_at) return false;
  return !b.last_success_at || Date.parse(b.last_failure_at) > Date.parse(b.last_success_at);
}

/** The backup's state in one chip. A failure since the last good set wins over its age. */
export function backupLook(b: Backup, span: BackupFormat["span"]): Look {
  if (b.state === "not_configured") return { tone: "unreviewed", glyph: "○", label: "Not configured" };
  if (failedSinceGood(b)) return { tone: "overdue", glyph: "⊘", label: "Failed" };
  if (b.state === "stale") return { tone: "overdue", glyph: "!", label: "Stale" };
  if (b.state === "never") return { tone: "unreviewed", glyph: "○", label: "No backup yet" };
  return { tone: "confirmed", glyph: "✓", label: `Backed up ${span(b.age_seconds ?? 0)} ago` };
}

/** "512 bytes", "3.4 MB", "1.2 GB" (powers of 1024, as `du -h` and `ls -lh` count). */
export function bytes(n: number) {
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return unit === 0 ? `${n} ${n === 1 ? "byte" : "bytes"}` : `${value.toLocaleString("en", { maximumFractionDigits: 1 })} ${units[unit]}`;
}

const takeOne = <>{code("manage.py backup_now --inline")} in the worker (Compose: {code(`${COMPOSE_WORKER} backup_now --inline`)})</>;

/** Each warning's chip and its fix, in doctor's words (core/doctor.check_backup). An unknown code shows its message. */
function warningLook(w: Warning, b: Backup): Look & { fix: React.ReactNode } {
  switch (w.code) {
    case "backup_stale":
      return b.state === "never"
        ? { tone: "pending", glyph: "◐", label: "No backup yet", fix: <>Take one now: {takeOne}. Until then nothing can be restored.</> }
        : { tone: "overdue", glyph: "!", label: "Backup stale", fix: <>Check the worker above and the worker log, then take one: {takeOne}.</> };
    case "backup_failed":
      return {
        tone: "overdue", glyph: "⊘", label: "Backup failed",
        fix: <>The worker log has the details (Compose: {code("docker compose logs worker | grep backup")}); the backup runbook explains each stage. A refusal for free space means moving the sets to another disk, not lowering the floor.</>,
      };
    case "backup_key_missing":
      return {
        tone: "overdue", glyph: "!", label: "No backup key",
        fix: <>Check {code("SOFTWARDEN_BACKUP_KEY_FILE")}, or the {code("backup_key")} in the secrets (Compose: {code("docker compose up -d")}, the init service generates it; Helm: the keys Secret).</>,
      };
    case "backup_key_unacknowledged":
      return {
        tone: "pending", glyph: "◐", label: "Key only on this host",
        fix: <>Copy it off the host (docs/INSTALL.md, step 6): {code("docker compose cp worker:/data/secrets/backup_key ./backup.key")}, put that copy in your password manager or vault, away from the backups, and delete it from this machine. Then confirm here. Lose the key and every backup is unreadable.</>,
      };
    // stored_secrets_unreadable isn't a backup warning: system.tsx lists it on its own.
    default:
      return { tone: "pending", glyph: "!", label: "Backup warning", fix: null };
  }
}

function KeyState({ b, tz, fmt }: { b: Backup; tz: string; fmt: BackupFormat }) {
  const focusable = { id: KEY_STATE_ID, tabIndex: -1, className: "inline-flex flex-wrap items-center gap-2 rounded-sm outline-none focus:ring-2 focus:ring-ring" };
  if (!b.key_present) return <span {...focusable}><GapChip label="No backup key file" /><span className="text-sm text-muted-foreground">Backups fail without it.</span></span>;
  if (b.key_source === "configured") {
    return <span {...focusable}><Chip tone="confirmed" glyph="✓" label="Your own key file" /><span className="text-sm text-muted-foreground">Set with {code("SOFTWARDEN_BACKUP_KEY_FILE")}.</span></span>;
  }
  if (b.key_acknowledged) {
    return (
      <span {...focusable}>
        <Chip tone="confirmed" glyph="✓" label="Copy confirmed off the host" />
        <span className="text-sm text-muted-foreground">Generated on this host{b.key_acknowledged_at ? <>; confirmed <time dateTime={b.key_acknowledged_at} className="tabular">{fmt.orgTime(b.key_acknowledged_at, tz)}</time></> : null}.</span>
      </span>
    );
  }
  return <span {...focusable}><Chip tone="pending" glyph="◐" label="Not confirmed copied off the host" /><span className="text-sm text-muted-foreground">Generated on this host by the init service.</span></span>;
}

/** Where the key's acknowledgement goes and which status it updates: the System page's (`system`), or the platform
 *  console's (`platform/system`, staff with no membership). */
type Where = { acknowledgePath: "system/backup/acknowledge-key" | "platform/system/backup/acknowledge-key"; statusPath: "system" | "platform/system" };

export function BackupPanel({ backup: b, warnings, tz, fmt, acknowledgePath = "system/backup/acknowledge-key", statusPath = "system" }: {
  backup: Backup; warnings: Warning[]; tz: string; fmt: BackupFormat;
} & Partial<Where>) {
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState("");
  const look = backupLook(b, fmt.span);
  const failed = failedSinceGood(b);
  const words = fmt.cronInWords(b.cron);
  const when = (iso: string) => <time dateTime={iso} className="tabular">{fmt.orgTime(iso, tz)}</time>;
  return (
    <Panel id="h-backups" icon={<HardDriveDownload className="size-4" aria-hidden="true" />} title="Backups" note="Installation-wide; the worker takes them">
      <div className={notice ? "px-4 pt-3" : undefined}><SuccessNote text={notice} /></div>
      {warnings.length > 0 && (
        <div className="border-b border-border-subtle px-4 py-3" data-testid="backup-warnings">
          <h3 className="mb-2 text-sm font-semibold">To fix ({warnings.length})</h3>
          <ul className="m-0 grid list-none gap-3 p-0" aria-label="Backup warnings">
            {warnings.map((w) => {
              const shown = warningLook(w, b);
              return (
                <li key={w.code} className="grid gap-1" data-code={w.code}>
                  <span className="flex flex-wrap items-center gap-2"><Chip {...shown} /><span className="font-medium">{w.message}</span></span>
                  {shown.fix && <p className="max-w-prose text-sm text-muted-foreground">{shown.fix}</p>}
                  {w.code === "backup_key_unacknowledged" && (
                    <div><Button type="button" size="sm" variant="outline" onClick={() => setConfirming(true)}>I have copied the backup key somewhere safe…</Button></div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <DetailList>
        <DetailRow label="State">
          <span className="flex flex-wrap items-center gap-2" data-testid="backup-state">
            <Chip {...look} />
            {b.running && <Chip tone="neutral" glyph="▸" label="Running now" />}
            {failed && b.last_failure && <span className="[overflow-wrap:anywhere]">{b.last_failure}</span>}
            {!failed && b.state === "stale" && <span>The last good backup is {fmt.span(b.age_seconds ?? 0)} old (stale after {fmt.span(b.stale_after_seconds)}).</span>}
          </span>
          {b.state === "not_configured" && (
            <p className="mt-1 max-w-prose text-sm text-muted-foreground">
              The worker takes no backups: {code("SOFTWARDEN_BACKUP_DIR")} is empty (the image sets {code("/data/backups")}). Set it to a mounted volume (Helm: {code("persistence.backups.enabled")}), or take backups with {code("ops/backup.sh")} on the host (backup runbook).
            </p>
          )}
        </DetailRow>
        <DetailRow label="Last good backup">
          {b.last_success_at
            ? <>{when(b.last_success_at)} <span className="text-muted-foreground">({fmt.span(b.age_seconds ?? 0)} ago)</span></>
            : <span className="text-muted-foreground">None yet</span>}
        </DetailRow>
        <DetailRow label="Set">
          {b.last_set
            ? <span className="flex flex-wrap items-center gap-x-3">{code(b.last_set)}
                {b.last_size_bytes != null && <span>{bytes(b.last_size_bytes)}</span>}
                {b.last_duration_seconds != null && <span className="text-muted-foreground">took {fmt.span(b.last_duration_seconds)}</span>}
              </span>
            : <span className="text-muted-foreground">None yet</span>}
        </DetailRow>
        <DetailRow label="Last failure">
          {b.last_failure_at
            ? <span className="[overflow-wrap:anywhere]">{when(b.last_failure_at)}{b.last_failure && <span className="block">{b.last_failure}</span>}{!failed && <span className="block text-sm text-muted-foreground">A good backup has been taken since.</span>}</span>
            : <span className="text-muted-foreground">None recorded</span>}
        </DetailRow>
        <DetailRow label="Destination">
          {b.destination
            ? <>{code(b.destination)} <span className="text-sm text-muted-foreground">in the worker container</span></>
            : <GapChip label="No backup folder" />}
        </DetailRow>
        <DetailRow label="Runs">
          {words ?? code(b.cron)}
          {words && <span className="block font-mono text-xs text-muted-foreground">{b.cron}</span>}
        </DetailRow>
        <DetailRow label="Backup key"><KeyState b={b} tz={tz} fmt={fmt} /></DetailRow>
      </DetailList>
      <Modal title="Confirm the backup key is copied" open={confirming} onClose={() => setConfirming(false)}
        returnFocusFallback={() => document.getElementById(KEY_STATE_ID)}
        description="Confirm only once the key is stored off this host, away from the backups: in your password manager or vault, held by the data contact and one other person.">
        <AcknowledgeForm acknowledgePath={acknowledgePath} statusPath={statusPath} onCancel={() => setConfirming(false)}
          onDone={(text) => { setConfirming(false); setNotice(text); }} />
      </Modal>
    </Panel>
  );
}

function AcknowledgeForm({ acknowledgePath, statusPath, onCancel, onDone }: Where & { onCancel: () => void; onDone: (text: string) => void }) {
  const client = useQueryClient();
  const invalidate = useInvalidate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // A 409: nothing to acknowledge (the key is configured by file, or doesn't exist yet). Only Done is left.
  const [refused, setRefused] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const backup = await (acknowledgePath === "platform/system/backup/acknowledge-key" ? typedApi("post", "platform/system/backup/acknowledge-key", {}) : typedApi("post", "system/backup/acknowledge-key", {}));
      // The answer is the new backup block: shown before the dialog closes, so focus lands on the key's new state
      // rather than a button about to go. The status is then reloaded in the background; the dialog used to wait for
      // that whole reload, and stayed open for seconds after the confirmation was recorded on a busy server.
      const key = apiKey(statusPath);
      await client.cancelQueries({ queryKey: key, exact: true }); // a reload begun before the confirmation is stale
      client.setQueryData<SystemStatus>(key, (old) => old && {
        ...old,
        backup,
        warnings: backup.key_acknowledged ? old.warnings.filter((w) => w.code !== "backup_key_unacknowledged") : old.warnings,
      });
      void invalidate(statusPath);
      onDone("The backup key is confirmed as copied off the host. The warning stays cleared until the key changes; the confirmation is in the audit log.");
    } catch (err) {
      setBusy(false);
      if (err instanceof Problem && err.status === 409) {
        setRefused(true);
        setError(`${err.detail} Nothing was changed; the status below has been reloaded.`);
        void invalidate(statusPath);
      } else {
        setError(`${problemText(err, "Couldn't record the confirmation.")} Nothing was changed. Try again.`);
      }
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert"><span aria-hidden="true">! </span>{error}</p>}
      {!refused && (
        <p className="mb-4 text-sm">Without the key every backup is unreadable, and the generated key lives on the same host as the backups. The confirmation is recorded in the audit log of every organization on this installation. A new key brings the warning back.</p>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{refused ? "Done" : "Cancel"}</Button>
        {!refused && <Button type="submit" disabled={busy}>{busy ? "Confirming…" : "Confirm the key is copied"}</Button>}
      </DialogFooter>
    </form>
  );
}
