# First run: set up Microsoft Entra discovery

This guide connects Softwarden to your Microsoft Entra tenant, so it can list the enterprise applications and app registrations you already have. It continues [the install guide](INSTALL.md): you start signed in as the first Admin, and you finish with an Entra connector that has run once and filled the Discovery queue.

**About 12 minutes.** With the install's 17 minutes, that is 29 minutes from `git clone`.

| Step | Where | Minutes |
|---|---|---|
| 1. Register an app | Microsoft Entra admin center | 3 |
| 2. Grant its permission | Microsoft Entra admin center | 2 |
| 3. Create a client secret | Microsoft Entra admin center | 1 |
| 4. Connect and test | Softwarden, Discovery › Set up a discovery source | 2 |
| 5. Scope and check access | Softwarden | 2 |
| 6. Run it and check | Softwarden | 2 |

Discovery only reads. Softwarden never writes to your tenant and never grants permissions itself. It also never changes your catalog on its own: what it finds waits in a queue for someone to link, create or dismiss. [Microsoft permissions](PERMISSIONS.md) lists every permission, every call and why.

**Before you start:**

- You need a person who can grant admin consent to Microsoft Graph application permissions: a Privileged Role Administrator (or Global Administrator). Cloud Application Administrators and Application Administrators can't grant Microsoft Graph application permissions (Microsoft Learn, "Grant tenant-wide admin consent to an application", prerequisites), though they can create the registration and the secret.
- Not ready to touch the real tenant yet? Use the tenant ID `simulated` with any client ID and secret in step 4. Softwarden then reads a synthetic tenant and contacts nobody. Skip steps 1 to 3. The server must allow it: set `SOFTWARDEN_ALLOW_SIMULATED=1` in `.env` and `docker compose up -d` ([ENV.md](ENV.md)); without it the tenant ID is refused, because the synthetic data would look real in your catalog. Delete the simulated connector and connection, and remove the setting, before you connect the real tenant.

Softwarden's guided setup shows the same instructions on screen (its Prepare step), so you can follow either.

## 1. Register an app (3 min)

1. Open the Microsoft Entra admin center: <https://entra.microsoft.com>.
2. Go to **Entra ID › App registrations › New registration**.
3. Fill in:
   - **Name:** `Softwarden discovery`;
   - **Supported account types:** *Single tenant only – <your tenant>*;
   - **Redirect URI:** leave it empty (discovery signs in as the app, with no browser).
4. Choose **Register**.

Use a registration for discovery only, not the one people sign in with. Each registration then holds only its own permissions ([Microsoft permissions, "Principles"](PERMISSIONS.md#principles)).

## 2. Grant its permission (2 min)

1. On the registration, open **API permissions › Add a permission › Microsoft Graph › Application permissions**.
2. Tick `Application.Read.All`. It is required: enterprise applications and app registrations, with their owners and the end dates of their secrets and certificates.
3. Optionally, also tick:
   - `User.ReadBasic.All`: owners' names and emails. Without it, owners show as "owner not resolved".
   - `AgentIdentity.Read.All`: Entra Agent ID agent identities, read only when you turn them on in the scope.
   - `LicenseAssignment.Read.All`: only for the separate **Microsoft Entra licenses** source (seat counts). The same connection can serve both sources.
4. Choose **Add permissions**, then **Grant admin consent for <your tenant>**, and confirm. Each permission's status should read *Granted for <your tenant>*.

The exact permissions, why each is needed and the calls behind them are in [Microsoft permissions, "Discovery source: entra"](PERMISSIONS.md#discovery-source-entra).

## 3. Create a client secret (1 min)

1. Open **Certificates & secrets › Client secrets › New client secret**.
2. Give it a description and choose an expiry. Microsoft recommends less than 12 months, and the maximum is 24 months. Discovery reports app secrets that are about to expire, including this one.

   Microsoft Learn advises against client secrets in production and recommends certificates. Softwarden supports only client secrets today; certificate sign-in is a later item.
3. Choose **Add**, then copy the secret's **Value**, not its Secret ID. It is shown once.
4. From **Overview**, copy the **Directory (tenant) ID** and the **Application (client) ID**.

Keep the three values to hand for the next step only. Softwarden stores the secret encrypted and never shows it again.

## 4. Connect and test (2 min)

1. In Softwarden, open **Discovery** and choose **Set up a discovery source** (or **Add a source**), then **Microsoft Entra ID**.
2. **Prepare** repeats steps 1 to 3. Choose **Next**.
3. On **Connect**, add a Microsoft connection:
   - name it `Contoso tenant`;
   - paste the tenant ID, the client ID and the secret value;
   - choose **Test**.

A pass proves the tenant, client ID and secret. A failure names the cause and the fix, for example:

- the Secret ID pasted instead of its Value;
- an expired secret;
- an unknown tenant;
- consent not granted.

The test proves sign-in only. Step 5 proves the permissions.

## 5. Scope and check access (2 min)

1. **Scope:** keep the defaults. Microsoft's own first-party apps and disabled apps are skipped and counted, and agent identities are off. Choose a reviewer if someone other than the IT coordinator should get the queue.
2. **Check access:** Softwarden makes one small read per permission. It names any permission that is missing, and how to fix it. Optional permissions that aren't granted are reported but never block. Fix and check again until every required step passes.

## 6. Run it and check (2 min)

1. **Finish:** choose **Run now**. The worker starts the run within a minute and the page shows live progress, then the counts: seen, kept, and skipped per filter. A tenant with a few hundred apps takes about a minute.
2. To run it every day, set the connector's schedule to **Daily**. It runs after 02:00 in the organization's timezone.
3. On the host, `doctor` should now show the connector's last run. An email `WARN` may remain until SMTP is set ([install guide](INSTALL.md), step 4c):

```bash
docker compose exec app python manage.py doctor
#   OK    [contoso] discovery: Microsoft Entra ID  Last run succeeded 2026-11-07 10:42 UTC, 214 seen.
```

Discovery is configured. Open **Discovery** to work the queue. Each candidate offers **Link** to an existing record, **Create record** or **Dismiss** with a reason, and every decision has an Undo. Suggested owners are shown but never applied: owners change only through an ordinary edit. **Needs attention** shows a *Discovery candidates* row per connector, routed to its reviewer.

## Next

- **People and roles:** invite IT staff in Administration › Users & roles.
- **Email:** if `doctor` warns about email, set the installation's SMTP in `.env` ([ENV.md](ENV.md)), or choose Microsoft Graph or SMTP in Administration › Email settings. Graph `sendMail` needs `Mail.Send` scoped to one mailbox through Exchange RBAC for Applications, never as an Entra API permission ([Microsoft permissions, "Email via Microsoft Graph"](PERMISSIONS.md#email-via-microsoft-graph)).
- **Single sign-on:** Administration › Sign-in. Configure, test and activate. Keep [the SSO recovery runbook](04-development/sso-recovery-runbook.md) at hand.
- **More sources:** Entra licenses, shadow IT, SSO and MFA coverage and license usage, Azure, Power Platform and HaloITSM. [Microsoft permissions](PERMISSIONS.md) lists what each needs, and which have been tested only against simulators.
- **Before real data:** copy backups off the host and rehearse a restore ([the backup runbook](04-development/backup-runbook.md)).
