/** A link to a record's own page (src/lib/kinds.ts), or its name as plain text when its kind has no page in this build.
 *  Never a link to `#` or to another kind's page. */
import { Link } from "react-router-dom";
import { pathOf } from "@/lib/kinds";

export function RecordLink({ record, tab, className, children }: {
  record: { id: string; kind: string; name?: string };
  /** A tab on the record's page ("relationships"). */
  tab?: string;
  className?: string;
  /** Shown instead of the record's name. */
  children?: React.ReactNode;
}) {
  const to = pathOf(record, tab);
  const content = children ?? record.name;
  return to ? <Link to={to} className={className}>{content}</Link> : <span className={className}>{content}</span>;
}
