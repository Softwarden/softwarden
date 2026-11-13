/** One way to download an API file (exports, templates, error reports): fetch it with the session, then save it under
 *  the name the server gives in Content-Disposition (or `fallback`). Used by every export button, so the problem
 *  handling and the object-URL clean-up live in one place. */
import { useState } from "react";

/** A refused download: the status and the problem body, so a caller can read `type` or field errors. */
export class DownloadError extends Error {
  constructor(public status: number, message: string, public body: Record<string, unknown> = {}) {
    super(message);
  }
}

/** The file name from a Content-Disposition header, or null. */
export function dispositionName(header: string | null): string | null {
  return /filename="?([^";]+)"?/.exec(header ?? "")?.[1] ?? null;
}

/** Fetch `/api/v1/<path>` and save it. Returns the saved file's name; throws DownloadError when the API refuses it (its
 *  detail, else field errors, else a sentence per status) or the connection drops. */
export async function saveDownload(path: string, fallback: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`/api/v1/${path}`, { credentials: "same-origin" });
  } catch {
    throw new DownloadError(0, "The download failed: the connection dropped before the file arrived.");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({})) as Record<string, unknown> & { errors?: unknown; detail?: string };
    const fields = Object.values((data.errors ?? {}) as Record<string, string[]>).flat().join(" ");
    const text = fields || data.detail || (res.status === 410 ? "The file was deleted after 30 days." : "The download failed.");
    throw new DownloadError(res.status, text, data);
  }
  // A 2xx web page is a proxy or sign-in gateway answering instead of Softwarden: saving it as the export would
  // hand someone an HTML file named .csv. Exports are never HTML.
  if ((res.headers.get("Content-Type") ?? "").includes("text/html")) {
    throw new DownloadError(res.status, "The download failed: something in front of Softwarden (a proxy or sign-in gateway) answered instead of it. Reload the page.");
  }
  const name = dispositionName(res.headers.get("Content-Disposition")) ?? fallback;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return name;
}

/** `download(path, fallback)` with a busy flag and the error to show ("… Nothing was downloaded. Try again."). Resolves
 *  to the saved name, or null after an error. */
export function useDownload() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function download(path: string, fallback: string): Promise<string | null> {
    setBusy(true);
    setError("");
    try {
      return await saveDownload(path, fallback);
    } catch (e) {
      setError(`${e instanceof Error ? e.message : "The download failed."} Nothing was downloaded. Try again.`);
      return null;
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, download };
}
