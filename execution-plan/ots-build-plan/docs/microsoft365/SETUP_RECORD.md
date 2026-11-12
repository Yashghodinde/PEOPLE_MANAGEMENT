# Microsoft 365 / Graph Setup Record

Record of connecting OTS to Microsoft 365. Completed on 2026-11-11 by the Global Administrator (COS@troporganic.com).
Follows `docs/GUIDE_CONNECT_ONEDRIVE.md`. Contains IDs only; no secrets. `graph.key` is never stored here.

## Design (fixed)

- One private SharePoint site, `TaskSystem`, holds OTS files and backups.
- OTS signs in as an app registration using a certificate. No client secret, no passwords.
- The app holds only Microsoft Graph application permission `Sites.Selected` (admin consent granted), plus a per-site `write` grant on `TaskSystem`.
- Never add `Sites.ReadWrite.All`, `Files.ReadWrite.All` or anything broader.

## Values for OTS

| Variable | Value |
|---|---|
| GRAPH_TENANT_ID | `4f3495ad-565f-47e9-b886-32be05a795b3` |
| GRAPH_CLIENT_ID | `c132e89f-d095-4079-b456-d7becba31429` |
| GRAPH_SITE_ID | `troporganic.sharepoint.com,cdf5df7c-9581-4261-9dc9-0bbad2527a89,82602c2a-60b5-4b7e-be5a-da60f865403f` |
| GRAPH_DRIVE_BUSINESS | `b!fN_1zYGVYUKdyQu60lJ6iSosYIK1YH5LvlraYPhlQD8P0jwGz0qfSZU-jKxF746E` |
| GRAPH_DRIVE_DR | `b!fN_1zYGVYUKdyQu60lJ6iSosYIK1YH5LvlraYPhlQD-z75oOkzyhTbv0RleNSdT8` |

## Step log

| Step | What | Result |
|---|---|---|
| 1 | SharePoint team site `TaskSystem` (Standard team, Private group) | Done. URL `https://troporganic.sharepoint.com/sites/TaskSystem`. Owner: only COS@troporganic.com, no other members. M365 group id `3abb1dcc-a69b-4066-8c4c-c07ba84cd7f1`. |
| 2 | Document libraries `Business-Files` and `System-DR` (default `Documents` left alone) | Done. |
| 3 | Entra app registration `Office Task System`, single tenant, no redirect URI | Done. Object ID `f09defcb-1930-443d-ba8f-7baba99bf059` (not used by OTS). |
| 4 | API permission `Sites.Selected` (Application) with admin consent | Done. Status "Granted for TROP ORGANIC". `User.Read` (Delegated) is the default and was left as is. |
| 5 | Upload public certificate `graph.cer` | Done. Thumbprint `B274B3608718EC6438CE36C97A6639965F648E10` matches. Certificate ID `e5083dce-22a8-4ae8-b22a-5273247cbe21`, expires 2028-11-08. 0 client secrets. |
| 6a | Graph Explorer consent to `Sites.FullControl.All` (delegated, one-time, setup only) | Done. |
| 6b | Site ID lookup | Done. Used `GET /groups/{group id}/sites/root` because `sites/{host}:/sites/TaskSystem` returned 400 and `sites?search=` returned empty (new site not yet indexed). |
| 6c | `POST /sites/{site id}/permissions`, role `write`, app Office Task System | Done. Permission id `aTowaS50fG1zLnNwLmV4dHxjMTMyZTg5Zi1kMDk1LTQwNzktYjQ1Ni1kN2JlY2JhMzE0MjlANGYzNDk1YWQtNTY1Zi00N2U5LWI4ODYtMzJiZTA1YTc5NWIz`. |
| 6d | Drive IDs for `Business-Files` and `System-DR` | Done. `Documents` drive also exists (`b!fN_1zYGVYUKdyQu60lJ6iSosYIK1YH5LvlraYPhlQD8CDmNq8-w9Sq1wakVb7JBO`), not used by OTS. |

## Troubleshooting notes (from this setup)

- Step 6c returned 400 "Invalid request" twice. Cause: Graph Explorer was still using the sign-in token from before the `Sites.FullControl.All` consent. Fix: sign out of Graph Explorer, sign in again, re-run the same request. No broader permission was added.
- The original request body with `grantedToIdentities` works. The response echoes both `grantedToIdentities` and `grantedToIdentitiesV2`.

## Follow-ups

- Revoke the temporary `Sites.FullControl.All` consent for Graph Explorer once step 6d is done (optional hygiene).
- The certificate expires on 2028-11-08. Rotate before then.
- Copy the five values above into `.dev/etc/.env` (dev) and later `/etc/ots/.env` (server).
- Run the Graph verification gate (G1-G9) to confirm read/write on both drives.
