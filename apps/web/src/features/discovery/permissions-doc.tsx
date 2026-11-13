/** Discovery › Permissions reference: docs/PERMISSIONS.md inside the app, so the walkthrough's links
 *  work on any installation without the repository at hand. The file is bundled into this page's chunk as text at
 *  build time and rendered by a small Markdown reader (markdown.ts) as React elements only (headings with anchors,
 *  paragraphs, lists, tables, code blocks, inline code, bold and links), never HTML. Every role may read it: it names
 *  permissions, not anyone's values. Loaded on first use. */
import { Link } from "react-router-dom";
// `@docs` is the repository's docs/ folder in development (vite.config.ts) and /docs in the Docker build stage.
import doc from "@docs/PERMISSIONS.md?raw";
import { Breadcrumb, PageHead } from "@/components/app";
import { MarkdownDoc } from "@/features/discovery/markdown-doc";

export function PermissionsReferencePage() {
  return (
    <>
      <Breadcrumb trail={[{ to: "/discovery", label: "Discovery" }, { to: "/discovery/setup", label: "Set up a source" }]} current="Permissions reference" />
      <PageHead title="Permissions reference" desc={<>Every permission Softwarden asks for, why, and how it was checked: docs/PERMISSIONS.md as shipped with this version. File paths in it refer to the source code. <Link className="underline" to="/discovery/setup">Back to the guided setup</Link>.</>} />
      <MarkdownDoc source={doc} testId="permissions-doc" />
    </>
  );
}
