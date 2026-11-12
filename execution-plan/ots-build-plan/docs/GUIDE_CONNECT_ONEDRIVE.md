# Guide: connect OTS to Microsoft 365 (OneDrive / SharePoint)

OTS stores files and backups in a **SharePoint site** inside your Microsoft 365 tenant (decision D-003). This is what
Microsoft calls "OneDrive for Business" storage for a team. OTS signs in as an **app with a certificate**, not as a
person, and can reach **only this one site** (permission `Sites.Selected`). No passwords or client secrets are stored.

You do steps 1 to 6 in the browser (about 45 minutes). Claude does step 7 onwards.

**Never paste into chat:** `graph.key` (private key), passwords, or secrets. IDs (tenant, client, site) are safe to share.

## What you need first
- A Microsoft 365 tenant with SharePoint Online (Business Basic / Standard / Premium, or E1/E3).
- An account that is **Global Administrator** (or SharePoint Administrator + Application Administrator).
- Storage reminder: SharePoint gives 1 TB plus 10 GB per licensed user for the whole tenant.

## Step 1. Create the SharePoint site
1. Open https://admin.microsoft.com, then **Show all > SharePoint** (or go to `https://<tenant>-admin.sharepoint.com`).
2. **Sites > Active sites > Create > Team site** (or Communication site).
3. Name: `TaskSystem`. Privacy: **Private**. Owner: the Super Admin only. Do not add other members.
4. Open the site (`https://<tenant>.sharepoint.com/sites/TaskSystem`).

## Step 2. Create the two document libraries
In the site: **New > Document library**, twice:
- `Business-Files` (uploaded work files go here)
- `System-DR` (database and backup copies go here; hidden from the app UI)

Leave the default `Documents` library alone. Spelling and capitals matter.

## Step 3. Register the app in Microsoft Entra
1. Open https://entra.microsoft.com > **Entra ID > App registrations > New registration**.
2. Name `Office Task System`. Supported account types: **Single tenant only**. No redirect URI. Register.
3. On the Overview page copy two values:
   - **Application (client) ID** → `GRAPH_CLIENT_ID`
   - **Directory (tenant) ID** → `GRAPH_TENANT_ID`

## Step 4. Give the app the `Sites.Selected` permission
1. In the app: **API permissions > Add a permission > Microsoft Graph > Application permissions**.
2. Search `Sites.Selected`, tick it, **Add permissions**.
3. Click **Grant admin consent for <your tenant>** and confirm. The status column must show a green tick.
4. Do **not** add `Sites.ReadWrite.All`, `Files.ReadWrite.All` or anything else. Those are too broad.

## Step 5. Upload the certificate
The certificate already exists on this Mac (created by `deploy/scripts/make-graph-cert.sh`):

```
project code/.dev/etc/graph.cer     <- PUBLIC half, upload this
project code/.dev/etc/graph.key     <- PRIVATE half, stays on the Mac, never uploaded or shared
```

1. In the app: **Certificates & secrets > Certificates > Upload certificate**.
2. Choose `graph.cer`. Add.
3. Check the **thumbprint** shown equals the one the script printed:
   `B274B3608718EC6438CE36C97A6639965F648E10` (re-run the script with `--force` for a new one).
4. Do not create a client secret.

## Step 6. Grant the app access to the TaskSystem site only
`Sites.Selected` gives access to nothing until an admin grants a site. Use Graph Explorer (no scripts needed):

1. Open https://developer.microsoft.com/graph/graph-explorer and sign in as the Global Administrator.
2. Click the profile icon > **Consent to permissions** and consent to **`Sites.FullControl.All`** (delegated). This is a
   one-time admin action only for this step; the OTS app never gets it.
3. Run **GET** `https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/TaskSystem`
   and copy the `id` (looks like `contoso.sharepoint.com,1111...,2222...`) → `GRAPH_SITE_ID`.
4. Run **POST** `https://graph.microsoft.com/v1.0/sites/<site id>/permissions` with this body (use your client ID):
   ```json
   {
     "roles": ["write"],
     "grantedToIdentities": [
       { "application": { "id": "<Application (client) ID>", "displayName": "Office Task System" } }
     ]
   }
   ```
   A `201 Created` response means success.
5. Run **GET** `https://graph.microsoft.com/v1.0/sites/<site id>/drives` and copy the `id` of each library:
   `Business-Files` → `GRAPH_DRIVE_BUSINESS`, `System-DR` → `GRAPH_DRIVE_DR`.

## Step 7. Give Claude these values (all safe to share)
```
GRAPH_TENANT_ID      = ...
GRAPH_CLIENT_ID      = ...
GRAPH_SITE_ID        = ...
GRAPH_DRIVE_BUSINESS = ...
GRAPH_DRIVE_DR       = ...
```
Claude then puts them in `.dev/etc/.env` (git-ignored) and runs the **Graph verification gate**, nine checks:

| Check | What it proves |
| --- | --- |
| G1 | The app can sign in with the certificate |
| G2 | It can list both libraries |
| G3 | It is **denied** on any other site (least privilege works) |
| G4 / G5 | Small upload works; a duplicate name is refused (`409`) |
| G6 | A 30 MiB resumable upload survives a killed process and resumes |
| G7 | A download matches the uploaded file byte for byte |
| G8 | Change tracking (delta) sees a new file |
| G9 | Test folders are cleaned up |

If G2, G4 or G6 return `403`, repeat step 6 (the grant). Never "fix" it by adding broader permissions.

## Safety notes
- The private key never leaves this Mac / the server. It is not in git (`.dev/` is ignored).
- Certificates expire (this one: 8 Nov 2028). OTS warns 30 days before. Rotation steps are in Runbook OPS-5.
- To cut access at any time: delete the permission in Graph Explorer, or remove the certificate in Entra.
- Business files are never deleted by the app. Only recovery copies in `System-DR` are pruned (by design).
