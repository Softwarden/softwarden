/** The Attachments tab of the record pages and the vendor dialog. The panel itself is loaded on first
 *  use; this part only counts the attachments for the tab's label (the panel reads the same cached query). */
import type { Attachment, AttachmentParentType, Me } from "@/api";
import { lazyPart, LazyPart } from "@/components/app";
import { useApiQuery } from "@/lib/query";
import { attachmentsPath, type AttachmentParent } from "./paths";

const Panel = lazyPart(() => import("./attachments").then((m) => m.AttachmentsPanel));

export function AttachmentsTab({ parent }: { parent: AttachmentParent }) {
  return <LazyPart what="attachments"><Panel parent={parent} /></LazyPart>;
}

export const hasAttachments = (me: Me) => me.capabilities.includes("attachments");

/** "Attachments (3)" once the list is in, "Attachments" before; null without the attachments capability. */
export function useAttachmentsLabel(me: Me, type: AttachmentParentType, id: string): string | null {
  const on = hasAttachments(me);
  const list = useApiQuery<Attachment[]>(attachmentsPath(type, id), { enabled: on });
  if (!on) return null;
  return `Attachments${list.data ? ` (${list.data.length})` : ""}`;
}
