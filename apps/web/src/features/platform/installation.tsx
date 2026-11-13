/** The platform console's System page: what staff read without a membership, from
 *  `GET platform/system`. The same page as an organization's Admin > System status in its `installation` view (worker,
 *  schedules, installation-wide jobs, the backup with its key acknowledgement, and every warning, stored secrets
 *  included), with times in the browser's timezone since no organization's applies. */
import { REFRESH_MS, SystemStatusView } from "@/features/admin/system";
import { useTypedQuery } from "@/lib/openapi";

const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

export function InstallationSystemPage() {
  // Refetched every 30 s; TanStack Query pauses the interval while the browser tab is hidden.
  const q = useTypedQuery("platform/system", {}, { refetchInterval: REFRESH_MS, refetchIntervalInBackground: false });
  return (
    <SystemStatusView q={q} tz={browserZone()} viewer="platform"
      desc="The installation's background worker, its schedules, installation-wide jobs and backups, with every warning to fix. Each organization's own jobs are on its Admins' System status page." />
  );
}
