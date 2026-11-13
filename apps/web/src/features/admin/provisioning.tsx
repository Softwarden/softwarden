/** Administration › Provisioning. SCIM provisioning comes from an optional package that isn't part of this
 *  repository, so no organization has the `scim` capability and the navigation doesn't link here; opened by its
 *  address, the page says it isn't part of this release and calls nothing. Admin only (others see AdminOnly). Loaded
 *  on first use. */
import { AdminOnly, PageHead } from "@/components/app";

const TITLE = "Provisioning";
const WHAT = "SCIM provisioning, through which an identity provider such as Microsoft Entra ID creates and deactivates members";

export function ProvisioningPage() {
  return (
    <AdminOnly title={TITLE} what={WHAT}>
      <PageHead title={TITLE} />
      <p className="max-w-prose rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm" data-unavailable="">
        <span aria-hidden="true">⊘ </span><span className="font-semibold">SCIM provisioning isn't available.</span> It is planned for a later Enterprise edition and isn't part of this release. Admins add and remove members in Users &amp; roles.
      </p>
    </AdminOnly>
  );
}
