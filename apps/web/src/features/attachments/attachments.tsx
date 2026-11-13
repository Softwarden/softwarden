/** Attachments: files and links on a record (application, component, AI agent, integration) or a vendor.
 *  Loaded on first use (the detail pages' Attachments tab, the Vendors tab's dialog), so none of this is in the first
 *  bundle.
 *
 *  - The list, grouped by category: title with its kind (file or link), size, who added it and when. A file is a plain
 *    link to its `download_url` (the API answers with Content-Disposition: attachment, never inline); a link opens in
 *    a new tab.
 *  - Upload (Editors and Admins): multipart with a title and a category, a progress bar, and the organization's limits
 *    checked before sending (GET /organization `attachments`: the largest file, the allowed types, the quota). The
 *    API's field errors (`file`, `title`, `category`) show inline; a 413 (the installation's request limit), a 429
 *    (Retry-After) and the quota error each say what to do.
 *  - Add link (http and https only), Edit (title, category, a link's address) with If-Match, and Remove, which says
 *    how long a removed file is kept.
 *  Viewers see the list and downloads only. */
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Link2 } from "lucide-react";
import {
  api, apiWithEtag, ATTACHMENT_CATEGORIES, Problem, problemFrom, writeHeaders, type Attachment, type AttachmentCategory, type Organization,
} from "@/api";
import { ErrorBanner, ErrorSummary, Field, Loading, Select, useReturnFocus } from "@/components/app";
import { MoreMenu } from "@/components/more-menu";
import { Panel, SuccessNote } from "@/components/sw";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { apiKey, problemText, useApiQuery } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";
import { cn } from "@/lib/utils";
import { attachmentsPath, sizeText, type AttachmentParent } from "./paths";

const MB = 1024 * 1024;
/** With no limits known (an older API, or GET organization failed), a dropped upload of a file over this size is most
 *  likely the installation's request limit (Caddy closes the connection). */
const UNKNOWN_LIMIT_HINT_MB = 5;
const IconAttachments = <FileText className="size-4" aria-hidden="true" />;

const day = (iso: string) => iso.slice(0, 10);
const extensionOf = (name: string) => (name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toLowerCase() : "");

/** What a refused write says, keyed by the API's field names. */
type Errors = Record<string, string[]>;
/** The forms' field IDs: the API's names with a prefix, so they never clash with IDs elsewhere on the page. */
const fid = (key: string) => `attachment-${key}`;
/** The API's keys (`file`, `title`…) as this form's field IDs, so each error shows under its field and the summary's
 *  links focus it. */
const shownErrors = (errors: Errors): Errors => Object.fromEntries(Object.entries(errors).map(([k, v]) => [fid(k), v]));

/** Every query that shows something an attachment changes: the list, the parent (its documentation check), data quality
 *  and Needs attention. */
function useRefresh(parent: AttachmentParent) {
  const client = useQueryClient();
  return () => Promise.all([
    attachmentsPath(parent.type, parent.id),
    parent.type === "vendor" ? "vendors" : attachmentsPath(parent.type, parent.id).replace(/\/attachments$/, ""),
    "data-quality", "needs-attention", "organization", "search",
  ].map((p) => client.invalidateQueries({ queryKey: apiKey(p) })));
}

/** The organization's attachment limits, or null while they load (or for an older API). */
function useLimits() {
  return useLimitsState().limits;
}

/** The limits and whether the request for them has finished (loaded or failed). */
function useLimitsState() {
  const org = useApiQuery<Organization>("organization", { staleTime: 30_000 });
  return { limits: org.data?.attachments ?? null, settled: !org.isPending };
}

/** "25.0 MB" near the limit, so a file just over it never reads as the limit itself; else sizeText. Past one decimal,
 *  the exact byte count. */
function sizeNear(bytes: number, limitMb: number) {
  if (bytes - limitMb * MB >= MB) return sizeText(bytes);
  const mb = (bytes / MB).toFixed(1);
  return Number(mb) > limitMb ? `${mb} MB` : `${mb} MB (${bytes.toLocaleString()} bytes)`;
}

export function AttachmentsPanel({ parent }: { parent: AttachmentParent }) {
  const me = useMe();
  const write = canWrite(me);
  const list = useApiQuery<Attachment[]>(attachmentsPath(parent.type, parent.id));
  const limits = useLimits();
  const [dialog, setDialog] = useState<{ kind: "upload" | "link" } | { kind: "edit" | "delete"; item: Attachment } | null>(null);
  const [notice, setNotice] = useState("");
  const close = () => setDialog(null);
  const done = (text: string) => { setDialog(null); setNotice(text); };
  const items = list.data ?? [];
  const groups = ATTACHMENT_CATEGORIES.map((c) => ({ ...c, items: items.filter((a) => a.category === c.value) })).filter((g) => g.items.length);

  const actions = write ? (
    <span className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" onClick={() => setDialog({ kind: "upload" })}>Upload file</Button>
      <Button size="sm" variant="outline" onClick={() => setDialog({ kind: "link" })}>Add link</Button>
    </span>
  ) : undefined;
  return (
    <>
      <SuccessNote text={notice} />
      <Panel id="h-attachments" icon={IconAttachments} title="Attachments" count={list.data ? items.length : undefined} action={actions}>
        {limits && (
          <p className="border-b border-border-subtle px-4 py-2 text-sm text-muted-foreground" data-testid="attachment-limits">
            Files up to {limits.max_size_mb} MB: {limits.allowed_extensions.join(", ")}.
            {limits.quota_mb !== null && <> {sizeText(limits.used_bytes)} of {limits.quota_mb.toLocaleString()} MB used in this organization.</>}
          </p>
        )}
        {list.isError && <div className="p-4"><ErrorBanner message={`${problemText(list.error, "Couldn't load the attachments.")} Nothing was changed.`} onRetry={() => void list.refetch()} /></div>}
        {list.isPending && <div className="p-4"><Loading what="attachments" rows={2} /></div>}
        {list.data && items.length === 0 && (
          <div className="px-4 py-5">
            <p className="font-semibold">No attachments yet</p>
            <p className="mt-1 max-w-prose text-muted-foreground">
              {write
                ? `Attach contracts, runbooks, architecture diagrams or security reviews to ${parent.name}, as files or as links to where they live (SharePoint, a wiki).`
                : `Files and links attached to ${parent.name} appear here. Editors and Admins can add them.`}
            </p>
          </div>
        )}
        {groups.length > 0 && (
          <div className="relative overflow-x-auto" role="region" aria-label={`Attachments of ${parent.name} table`} tabIndex={0}>
            <table className="w-full caption-bottom text-sm">
              <caption className="sr-only">Attachments of {parent.name}, grouped by category, then by title</caption>
              <thead className="bg-table-head">
                <tr className="border-b">
                  {["Title", "Size", "Added by", "Added"].map((h) => <th key={h} scope="col" className="h-10 px-3 text-left font-semibold whitespace-nowrap text-muted-foreground">{h}</th>)}
                  <th scope="col" className="h-10 px-3"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              {groups.map((g) => (
                <tbody key={g.value} data-category={g.value}>
                  <tr className="border-b bg-muted">
                    <th scope="rowgroup" colSpan={5} className="px-3 py-1.5 text-left text-[13px] font-semibold">
                      {g.label} <span className="font-normal text-muted-foreground">({g.items.length})</span>
                    </th>
                  </tr>
                  {g.items.map((a) => (
                    <tr key={a.id} className="border-b border-border-subtle last:border-0 hover:bg-row-hover">
                      <th scope="row" className="max-w-md px-3 py-2.5 text-left align-top font-semibold text-foreground">
                        <span className="flex items-start gap-2">
                          <span className="mt-0.5 inline-grid size-6 shrink-0 place-items-center rounded-md bg-muted text-icon" aria-hidden="true">
                            {a.kind === "file" ? <FileText className="size-3.5" /> : <Link2 className="size-3.5" />}
                          </span>
                          <span className="min-w-0 [overflow-wrap:anywhere]">
                            <span className="sr-only">{a.kind === "file" ? "File: " : "Link: "}</span>{a.title}
                            <span className="block text-[13px] font-normal text-muted-foreground">{a.kind === "file" ? a.filename : hostOf(a.url)}</span>
                          </span>
                        </span>
                      </th>
                      <td className="px-3 py-2.5 align-top tabular whitespace-nowrap">{a.kind === "file" ? sizeText(a.size) : <span className="text-muted-foreground">Link</span>}</td>
                      <td className="px-3 py-2.5 align-top">{a.uploaded_by?.name ?? <span className="text-muted-foreground">Unknown</span>}</td>
                      <td className="px-3 py-2.5 align-top tabular whitespace-nowrap"><time dateTime={a.created_at}>{day(a.created_at)}</time></td>
                      <td className="px-3 py-2 text-right align-top">
                        <span className="inline-flex flex-wrap items-center justify-end gap-2">
                          <OpenAction a={a} />
                          {write && <MoreMenu size="sm" label="Actions" about={a.title} actions={[
                            { label: "Edit…", onSelect: () => setDialog({ kind: "edit", item: a }) },
                            { label: "Remove…", danger: true, onSelect: () => setDialog({ kind: "delete", item: a }) },
                          ]} />}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              ))}
            </table>
          </div>
        )}
      </Panel>
      <UploadDialog parent={parent} open={dialog?.kind === "upload"} onClose={close} onDone={done} />
      <LinkDialog parent={parent} open={dialog?.kind === "link"} onClose={close} onDone={done} />
      <EditDialog parent={parent} item={dialog?.kind === "edit" ? dialog.item : null} onClose={close} onDone={done} />
      <RemoveDialog parent={parent} item={dialog?.kind === "delete" ? dialog.item : null} onClose={close} onDone={done} />
    </>
  );
}

function hostOf(url: string) {
  try { const u = new URL(url); return u.host + (u.pathname === "/" ? "" : u.pathname); } catch { return url; }
}

/** Download a file (a plain link: the browser saves what the API sends as an attachment) or open a link. */
function OpenAction({ a }: { a: Attachment }) {
  const cls = buttonVariants({ variant: "outline", size: "sm" });
  if (a.kind === "file" && a.download_url) {
    return <a className={cn(cls, "no-underline")} href={a.download_url} download={a.filename || true}>Download<span className="sr-only"> {a.title}</span></a>;
  }
  let safe = false;
  try { safe = /^https?:$/.test(new URL(a.url).protocol); } catch { safe = false; }
  if (!safe) return null; // the API only stores http(s) links; anything else is never made a link here
  return <a className={cn(cls, "no-underline")} href={a.url} target="_blank" rel="noreferrer noopener">Open link<span className="sr-only"> {a.title} (opens in a new tab)</span></a>;
}

// ---- upload --------------------------------------------------------------------------------------------------------

/** POST multipart/form-data with upload progress (fetch can't report it), CSRF, and the API's problem errors.
 *  Resolves with the new attachment; rejects with a Problem (status 0 when the request never got an answer). */
export function uploadFile(path: string, form: FormData, onProgress: (fraction: number) => void, signal: AbortSignal): Promise<Attachment> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/v1/${path}`);
    xhr.setRequestHeader("Accept", "application/json");
    for (const [name, value] of Object.entries(writeHeaders())) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data: Record<string, unknown> = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { data = {}; }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(data as unknown as Attachment);
      const detail = typeof data.detail === "string" ? data.detail
        : xhr.status === 413 ? "The upload is larger than this installation accepts." : "Something went wrong.";
      reject(problemFrom(xhr.status, { ...data, detail }, Number(xhr.getResponseHeader("Retry-After"))));
    };
    xhr.onerror = () => reject(new Problem(0, "The upload didn't reach Softwarden. Check your connection and try again."));
    xhr.onabort = () => reject(new DOMException("Upload canceled", "AbortError"));
    signal.addEventListener("abort", () => xhr.abort());
    xhr.send(form);
  });
}

/** "about 3 minutes" for a Retry-After in seconds. */
function waitText(seconds?: number) {
  if (!seconds) return "a little while";
  if (seconds < 90) return `${seconds} seconds`;
  const minutes = Math.ceil(seconds / 60);
  return minutes < 90 ? `about ${minutes} minutes` : `about ${Math.ceil(minutes / 60)} hours`;
}

/** The organization's limits checked in the browser before anything is sent; the API checks them again. */
export function precheck(file: File, limits: Organization["attachments"] | null): string | null {
  if (!limits) return null;
  const ext = extensionOf(file.name);
  if (!ext || !limits.allowed_extensions.includes(ext)) {
    return `${ext ? `.${ext} files` : "Files without an extension"} can't be attached here. Allowed: ${limits.allowed_extensions.join(", ")}.`;
  }
  if (file.size > limits.max_size_mb * MB) return `The file is ${sizeNear(file.size, limits.max_size_mb)}, larger than this organization's ${limits.max_size_mb} MB limit.`;
  if (file.size === 0) return "The file is empty. Choose another file.";
  if (limits.quota_mb !== null && limits.used_bytes + file.size > limits.quota_mb * MB) {
    return `This file would take the organization past its ${limits.quota_mb.toLocaleString()} MB attachment quota (${sizeText(limits.used_bytes)} used). Remove some attachments first, or ask an Admin.`;
  }
  return null;
}

function DialogShell({ open, onClose, title, description, children }: {
  open: boolean; onClose: () => void; title: string; description: React.ReactNode; children: React.ReactNode;
}) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {open && children}
      </DialogContent>
    </Dialog>
  );
}

function CategorySelect({ id, value, onChange }: { id: string; value: string; onChange: (v: AttachmentCategory) => void }) {
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value as AttachmentCategory)}>
      {ATTACHMENT_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
    </Select>
  );
}

function UploadDialog({ parent, open, onClose, onDone }: { parent: AttachmentParent; open: boolean; onClose: () => void; onDone: (t: string) => void }) {
  return (
    <DialogShell open={open} onClose={onClose} title={`Upload a file to ${parent.name}`}
      description="The file is checked against its type and stored privately; anyone in this organization can download it.">
      <UploadForm parent={parent} onClose={onClose} onDone={onDone} />
    </DialogShell>
  );
}

function UploadForm({ parent, onClose, onDone }: { parent: AttachmentParent; onClose: () => void; onDone: (t: string) => void }) {
  const { limits, settled } = useLimitsState();
  const refresh = useRefresh(parent);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<AttachmentCategory>("other");
  const [errors, setErrors] = useState<Errors>({});
  const [banner, setBanner] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []); // closing the dialog cancels a running upload

  function choose(f: File | null) {
    setFile(f);
    setBanner("");
    const problem = f ? precheck(f, limits) : null;
    setErrors(problem ? { file: [problem] } : {});
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBanner("");
    if (!file) return setErrors({ file: ["Choose a file to upload."] });
    const problem = precheck(file, limits);
    if (problem) return setErrors({ file: [problem] });
    const form = new FormData();
    form.append("file", file);
    if (title.trim()) form.append("title", title.trim());
    form.append("category", category);
    abort.current = new AbortController();
    setErrors({});
    setProgress(0);
    try {
      const made = await uploadFile(attachmentsPath(parent.type, parent.id), form, setProgress, abort.current.signal);
      await refresh();
      onDone(`${made.title} uploaded (${sizeText(made.size)}).`);
    } catch (err) {
      setProgress(null);
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (!(err instanceof Problem)) return setBanner("Something went wrong. Nothing was uploaded; try again.");
      if (err.status === 413) return setErrors({ file: [`${err.detail} Choose a smaller file.`] });
      // No answer at all: a proxy drops an oversized request instead of answering 413.
      if (err.status === 0 && (limits ? file.size > limits.max_size_mb * MB : file.size > UNKNOWN_LIMIT_HINT_MB * MB)) {
        return setErrors({ file: [`The upload didn't reach Softwarden. At ${sizeText(file.size)} the file may be larger than this installation accepts; try a smaller file, or check your connection.`] });
      }
      if (err.status === 429) return setBanner(`Too many uploads from your account for now. Try again in ${waitText(err.retryAfter)}; nothing was uploaded.`);
      if (Object.keys(err.errors).length) return setErrors(err.errors);
      setBanner(`${err.detail} Nothing was uploaded.`);
    }
  }
  const uploading = progress !== null;
  const accept = limits?.allowed_extensions.map((x) => `.${x}`).join(",");
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={shownErrors(errors)} focusOnShow="field" />
      {banner && <ErrorBanner message={banner} />}
      <Field id={fid("file")} label="File (required)" errors={shownErrors(errors)}
        help={limits ? `Up to ${limits.max_size_mb} MB. Types: ${limits.allowed_extensions.join(", ")}.` : undefined}>
        <Input id={fid("file")} type="file" accept={accept} disabled={uploading} onChange={(e) => choose(e.target.files?.[0] ?? null)} />
      </Field>
      <Field id={fid("title")} label="Title" errors={shownErrors(errors)} help="Default: the file's name.">
        <Input id={fid("title")} value={title} maxLength={200} disabled={uploading} onChange={(e) => setTitle(e.target.value)} placeholder={file?.name} />
      </Field>
      <Field id={fid("category")} label="Category" errors={shownErrors(errors)}>
        <CategorySelect id={fid("category")} value={category} onChange={setCategory} />
      </Field>
      {uploading && (
        <div className="mb-3 flex flex-col gap-1.5">
          <label htmlFor="upload-progress" className="text-sm font-medium">Uploading {file?.name}</label>
          <progress id="upload-progress" max={100} value={Math.round((progress ?? 0) * 100)} className="sw-progress h-2 w-full" />
          <span className="text-sm text-muted-foreground tabular" aria-live="polite">{progress !== null && progress >= 1 ? "Checking the file…" : `${Math.round((progress ?? 0) * 100)}%`}</span>
        </div>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => { abort.current?.abort(); onClose(); }}>{uploading ? "Cancel upload" : "Cancel"}</Button>
        <Button type="submit" disabled={uploading || !settled}>{uploading ? "Uploading…" : settled ? "Upload" : "Loading limits…"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- links -----------------------------------------------------------------------------------------------------------

function LinkDialog({ parent, open, onClose, onDone }: { parent: AttachmentParent; open: boolean; onClose: () => void; onDone: (t: string) => void }) {
  return (
    <DialogShell open={open} onClose={onClose} title={`Add a link to ${parent.name}`}
      description="A web address where the document lives, such as a SharePoint or wiki page. Only http:// and https:// addresses.">
      <LinkForm parent={parent} onClose={onClose} onDone={onDone} />
    </DialogShell>
  );
}

/** A link's address checked in the browser: http(s), with no user name or password. The API checks it again. */
function checkUrl(url: string): string | null {
  const value = url.trim();
  if (!value) return "Enter the link's web address.";
  let u: URL;
  try { u = new URL(value); } catch { return "Enter a full web address starting with http:// or https://."; }
  if (!/^https?:$/.test(u.protocol)) return "Enter a full web address starting with http:// or https://.";
  if (u.username || u.password) return "Leave the user name and password out of the address.";
  return null;
}

function LinkForm({ parent, onClose, onDone }: { parent: AttachmentParent; onClose: () => void; onDone: (t: string) => void }) {
  const refresh = useRefresh(parent);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<AttachmentCategory>("other");
  const [errors, setErrors] = useState<Errors>({});
  const [banner, setBanner] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBanner("");
    const bad = checkUrl(url);
    if (bad) return setErrors({ url: [bad] });
    setSaving(true);
    try {
      const made = await api<Attachment>(attachmentsPath(parent.type, parent.id), {
        method: "POST", body: { kind: "link", url: url.trim(), ...(title.trim() ? { title: title.trim() } : {}), category },
      });
      await refresh();
      onDone(`Link ${made.title} added.`);
    } catch (err) {
      if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setBanner(`${problemText(err)} Nothing was saved.`);
    } finally {
      setSaving(false);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={shownErrors(errors)} focusOnShow="field" />
      {banner && <ErrorBanner message={banner} />}
      <Field id={fid("url")} label="Web address (required)" errors={shownErrors(errors)} help="For example https://contoso.sharepoint.com/sites/it/runbook.docx">
        <Input id={fid("url")} type="url" inputMode="url" value={url} maxLength={2000} onChange={(e) => setUrl(e.target.value)} />
      </Field>
      <Field id={fid("title")} label="Title" errors={shownErrors(errors)} help="Default: the web address.">
        <Input id={fid("title")} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
      </Field>
      <Field id={fid("category")} label="Category" errors={shownErrors(errors)}>
        <CategorySelect id={fid("category")} value={category} onChange={setCategory} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Adding…" : "Add link"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- edit and remove -------------------------------------------------------------------------------------------------

function EditDialog({ parent, item, onClose, onDone }: { parent: AttachmentParent; item: Attachment | null; onClose: () => void; onDone: (t: string) => void }) {
  // The last item stays named while the dialog closes.
  const shown = useRef(item);
  if (item) shown.current = item;
  return (
    <DialogShell open={item !== null} onClose={onClose} title={`Edit ${shown.current?.title ?? "attachment"}`}
      description={shown.current?.kind === "file" ? "Change the title or category. A file's content can't change: upload a new file and remove this one." : "Change the title, category or web address."}>
      {item && <EditLoader parent={parent} item={item} onClose={onClose} onDone={onDone} />}
    </DialogShell>
  );
}

/** Reads the attachment again with its ETag, so the save sends If-Match: a change saved by someone else since is refused
 *  (412) rather than overwritten, and "Load the latest version" restarts the form from it. */
function EditLoader({ parent, item, onClose, onDone }: { parent: AttachmentParent; item: Attachment; onClose: () => void; onDone: (t: string) => void }) {
  const path = `attachments/${item.id}`;
  const fresh = useQuery({ queryKey: [...apiKey(path), "etag"], queryFn: () => apiWithEtag<Attachment>(path), gcTime: 0, staleTime: Infinity, retry: false });
  const [round, setRound] = useState(0);
  const [stale, setStale] = useState(false);
  if (fresh.error) return <ErrorBanner message={problemText(fresh.error, "Couldn't load the attachment.")} onRetry={() => void fresh.refetch()} />;
  if (!fresh.data) return <Loading what="the attachment" />;
  async function loadLatest() {
    await fresh.refetch();
    setStale(false);
    setRound((n) => n + 1);
  }
  return (
    <>
      {stale && (
        <div className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
          <p className="font-semibold">Someone else changed {item.title} after this dialog opened, so your change wasn't saved.</p>
          <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => void loadLatest()}>Load the latest version</Button>
        </div>
      )}
      <EditForm key={round} parent={parent} item={fresh.data.data} etag={fresh.data.etag} onClose={onClose} onDone={onDone} onStale={() => setStale(true)} />
    </>
  );
}

function EditForm({ parent, item, etag, onClose, onDone, onStale }: {
  parent: AttachmentParent; item: Attachment; etag: string | null; onClose: () => void; onDone: (t: string) => void; onStale: () => void;
}) {
  const refresh = useRefresh(parent);
  const [title, setTitle] = useState(item.title);
  const [category, setCategory] = useState<AttachmentCategory>(item.category);
  const [url, setUrl] = useState(item.url);
  const [errors, setErrors] = useState<Errors>({});
  const [banner, setBanner] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBanner("");
    const body: Record<string, string> = {};
    if (title.trim() !== item.title) body.title = title.trim();
    if (category !== item.category) body.category = category;
    if (item.kind === "link" && url.trim() !== item.url) {
      const bad = checkUrl(url);
      if (bad) return setErrors({ url: [bad] });
      body.url = url.trim();
    }
    if (!title.trim()) return setErrors({ title: ["Enter a title."] });
    if (!Object.keys(body).length) return onClose();
    setSaving(true);
    try {
      await api(`attachments/${item.id}`, { method: "PATCH", body, headers: etag ? { "If-Match": etag } : undefined });
      await refresh();
      onDone(`${title.trim()} saved.`);
    } catch (err) {
      if (err instanceof Problem && err.status === 412) return onStale();
      if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setBanner(`${problemText(err)} Nothing was saved.`);
    } finally {
      setSaving(false);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={shownErrors(errors)} focusOnShow="field" />
      {banner && <ErrorBanner message={banner} />}
      {item.kind === "file" && <p className="mb-3 text-sm text-muted-foreground">File: {item.filename} ({sizeText(item.size)})</p>}
      <Field id={fid("title")} label="Title (required)" errors={shownErrors(errors)}>
        <Input id={fid("title")} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
      </Field>
      <Field id={fid("category")} label="Category" errors={shownErrors(errors)}>
        <CategorySelect id={fid("category")} value={category} onChange={setCategory} />
      </Field>
      {item.kind === "link" && (
        <Field id={fid("url")} label="Web address (required)" errors={shownErrors(errors)}>
          <Input id={fid("url")} type="url" inputMode="url" value={url} maxLength={2000} onChange={(e) => setUrl(e.target.value)} />
        </Field>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save changes"}</Button>
      </DialogFooter>
    </form>
  );
}

function RemoveDialog({ parent, item, onClose, onDone }: { parent: AttachmentParent; item: Attachment | null; onClose: () => void; onDone: (t: string) => void }) {
  const days = useLimits()?.retention_days;
  const shown = useRef(item);
  if (item) shown.current = item;
  const a = shown.current;
  return (
    <DialogShell open={item !== null} onClose={onClose} title={`Remove ${a?.title ?? "attachment"}?`}
      description={a?.kind === "file"
        ? `It disappears from ${parent.name} at once and can't be downloaded any more. The stored file is kept for the retention window${days ? ` of ${days} ${days === 1 ? "day" : "days"}` : ""}, so backups stay consistent, then deleted for good.`
        : `The link disappears from ${parent.name}. The page it points to isn't changed.`}>
      {item && <RemoveForm parent={parent} item={item} onClose={onClose} onDone={onDone} />}
    </DialogShell>
  );
}

function RemoveForm({ parent, item, onClose, onDone }: { parent: AttachmentParent; item: Attachment; onClose: () => void; onDone: (t: string) => void }) {
  const refresh = useRefresh(parent);
  const [sure, setSure] = useState(item.kind === "link");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Read with its ETag as the confirmation opens; the removal sends it as If-Match.
  const opened = useOpenedRecord(`attachments/${item.id}`);
  async function remove(e: React.FormEvent) {
    e.preventDefault();
    if (!sure) return setError("Tick the box to confirm the file can't be downloaded after this.");
    setBusy(true);
    try {
      await api(`attachments/${item.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      await refresh();
      onDone(`${item.title} removed.`);
    } catch (err) {
      setError(isStaleWrite(err) ? staleDeleteText("attachment", "remove") : `${problemText(err)} Nothing was removed.`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={remove} noValidate>
      {error && <ErrorBanner message={error} />}
      {item.kind === "file" && (
        <label className="mb-4 flex items-start gap-2 text-sm">
          <input type="checkbox" id="remove-confirm" checked={sure} onChange={(e) => { setSure(e.target.checked); setError(""); }} className="mt-0.5 h-4 w-4 accent-primary" />
          <span>I understand {item.filename} can't be downloaded after this.</span>
        </label>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" variant="destructive" disabled={busy}>Remove {item.title}</Button>
      </DialogFooter>
    </form>
  );
}
