/** Where a record's or vendor's attachments live, in the API and in the SPA. Small and eager: the
 *  detail pages count attachments for their tab label; the panel itself is loaded on first use. */
import type { AttachmentParentType } from "@/api";
import { apiPath, recordPath } from "@/lib/kinds";

export type AttachmentParent = { type: AttachmentParentType; id: string; name: string };

/** `GET`/`POST` path of the parent's attachments, relative to /api/v1/. */
export function attachmentsPath(type: AttachmentParentType, id: string): string {
  if (type === "vendor") return `vendors/${id}/attachments`;
  if (type === "contract") return `contracts/${id}/attachments`;
  return `${apiPath(type)}/${id}/attachments`;
}

/** The SPA page showing the parent's attachments: a record's, contract's or vendor's Attachments tab (the vendor
 *  page; the Vendors page's dialog still opens from `/vendors?attachments=<id>`). */
export function attachmentsPage(type: string, id: string): string | null {
  if (type === "vendor") return `/vendors/${id}?tab=attachments`;
  if (type === "contract") return `/contracts/${id}?tab=attachments`;
  return recordPath(type, id, "attachments");
}

/** "1.4 MB": bytes in KB, MB or GB (1024-based, as the API's limits are). */
export function sizeText(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} ${n === 1 ? "byte" : "bytes"}`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[i]}`;
}
