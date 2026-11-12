# 02 — P0: Infrastructure and Microsoft 365 Tenant (human tasks, days 0–3)

People do these steps, not Claude Code. Claude Code can write the scripts named here (see the prompts at the end). Running them needs physical or admin access.

| # | Task | Owner | Time | Output | Success check |
| --- | --- | --- | --- | --- | --- |
| P0.1 | Inspect the server | Office admin | 30 min | `server-inventory.txt` | Contains the output of `lsb_release -a`, `nproc`, `free -h`, `lsblk`, `df -h`, `smartctl -a /dev/sdX` and `ip a` |
| P0.2 | Install Ubuntu Server 24.04 LTS (minimal), if not already on it | Office admin | 1 h | Clean OS | `lsb_release -a` shows 24.04 |
| P0.3 | DHCP reservation for the server on the router | Office admin | 15 min | Fixed IP, e.g. `192.168.1.10` | IP is unchanged after a reboot |
| P0.4 | Local DNS `tasks.office.lan` if the router supports it | Office admin | 15 min | Hostname resolves | `nslookup tasks.office.lan` from a PC returns the server IP. If the router can't do this, use the IP everywhere |
| P0.5 | UPS installed, USB signal cable to server, Network UPS Tools (NUT) configured | Office admin | 1 h | Clean shutdown at 20% battery | Pull mains power: the server shuts down cleanly, and comes back up when power returns |
| P0.6 | Every PC: Chrome or Edge current version; webcam working | Office admin | 2 h | Inventory sheet | Camera test page works on every PC |
| P0.7 | Microsoft 365 tenant on the company domain; Global Admin with MFA; 1 Business Basic licence | Super Admin | 1 h | Tenant ready | Admin centre shows the licence |
| P0.8 | SharePoint site `TaskSystem` (private; owner = Super Admin only) with two document libraries, `Business-Files` and `System-DR` | Super Admin | 20 min | Site + libraries | Both libraries visible to the owner only |
| P0.9 | Entra app registration "Office Task System" (single tenant); application permission **Sites.Selected**; admin consent | Super Admin | 20 min | Client ID, Tenant ID | Consent granted |
| P0.10 | Certificate for the app: generate on the server (prompt P0-A), upload the public `.cer` to the app registration | Office admin + Super Admin | 15 min | `/etc/ots/graph.pem`, `/etc/ots/graph.key` (mode 600, root) | Thumbprint matches in Entra |
| P0.11 | Grant the app `write` on the `TaskSystem` site (script P0-B, run once by an admin) | Super Admin | 15 min | Grant ID | The script prints the grant |
| P0.12 | **Graph verification gate** (script P0-C) | Office admin | 30 min | `graph-gate-report.json` | All 9 checks PASS (below) |
| P0.13 | Generate the age backup key pair **on an offline laptop**. Copy only the public key to `/etc/ots/backup.pub`. Print the private key as a QR code, seal it in an envelope with the break-glass credentials, and store a second copy in the CEO's password manager | Super Admin | 20 min | Sealed key | Test: encrypt a file on the server, decrypt it on the laptop |
| P0.14 | Internal CA root certificate install script ready (installed on PCs in M00.8 once Caddy is running) | Office admin | — | — | — |
| P0.15 | Firewall baseline: `ufw default deny incoming`; allow 22 (key-only SSH), 80 and 443 from the office subnet only | Office admin | 15 min | — | `ufw status` shows these rules |
| P0.16 | Record the uplink speed (speedtest) | Office admin | 5 min | Mbps value | Value written into `.env` as `OTS_UPLINK_MBPS` |

## Graph verification gate (P0-C): all must pass

| # | Check | Pass |
| --- | --- | --- |
| G1 | Acquire a token with the certificate (`msal` ConfidentialClientApplication, client credentials) | Token received |
| G2 | `GET /sites/{site-id}/drives` | Lists both libraries; drive IDs recorded |
| G3 | `GET /sites/{other-site-id}` (any other site in the tenant) | **403 / accessDenied** (proves least privilege) |
| G4 | Simple upload of 1 KB to `Business-Files/_gate/small.txt` with `@microsoft.graph.conflictBehavior=fail` | 201 |
| G5 | Repeat G4 | **409** nameAlreadyExists |
| G6 | Resumable upload of 30 MiB, 5 MiB pieces, to `System-DR/_gate/large.bin`; kill the process after piece 3; resume from `nextExpectedRanges` | 201 + size matches |
| G7 | Download G6's file, compare SHA-256 | Match |
| G8 | Delta: `GET /drives/{dr}/root/delta`, then follow `@odata.deltaLink` after adding a file | The new item appears in the delta |
| G9 | Delete the `_gate` folders (cleanup, the only delete ever done to `Business-Files`) | 204 |

**If G2, G4 or G6 fail with 403:** re-run P0.11, granting `write` on the site. If it still fails, add `Lists.SelectedOperations.Selected` with grants on both libraries, log a decision, and re-run the gate. **Never** fall back to `Sites.ReadWrite.All` or `Files.ReadWrite.All`.

## Prompts for Claude Code (scripts only)

**Prompt P0-A (certificate script)**
```
Read CLAUDE.md and docs/plan/02_P0_INFRA_AND_TENANT.md.
Write deploy/scripts/make-graph-cert.sh. It must:
- create /etc/ots if it is missing (root:root, 0700);
- generate an RSA 2048 key pair plus a self-signed X.509 cert valid 365 days, CN "OTS Graph App";
- write /etc/ots/graph.key (0600) and /etc/ots/graph.pem (cert + key concatenated, 0600), plus /etc/ots/graph.cer (public cert only, 0644, for upload to Entra);
- print the SHA-1 thumbprint and the expiry date;
- refuse to overwrite existing files unless --force is given.
Add a shellcheck-clean header and usage text. No other changes.
```

**Prompt P0-B (site grant script)**
```
Write deploy/scripts/grant-site.py (standalone, uses msal + httpx). An admin runs it once, interactively, with the device-code flow, delegated scope Sites.FullControl.All, as a Global/SharePoint admin.
Inputs: --site-url https://<tenant>.sharepoint.com/sites/TaskSystem --app-id <client id> --app-name "Office Task System".
Steps:
1. Resolve the site ID via GET /sites/{hostname}:/sites/TaskSystem.
2. POST /sites/{site-id}/permissions with roles ["write"] and grantedToIdentities [{application:{id,displayName}}].
3. Print the site ID and the permission ID.
4. GET /sites/{site-id}/drives and print each drive's name and ID.
Idempotent: if a grant for the app already exists, print it and exit 0. No secrets are written to disk.
```

**Prompt P0-C (Graph gate)**
```
Write deploy/scripts/graph_gate.py: a standalone script using msal (certificate client credentials from GRAPH_* env vars) and httpx.
Implement checks G1–G9 exactly as specified in docs/plan/02_P0_INFRA_AND_TENANT.md:
- Each check prints PASS or FAIL with its HTTP status and timing.
- The script writes graph-gate-report.json.
- It exits non-zero on any FAIL.
- G6 must actually kill and resume the upload: run the upload in a subprocess, terminate it after piece 3, then resume using the saved uploadUrl and the GET status nextExpectedRanges.
- Honour Retry-After on 429 and 503.
Include --other-site-id for G3.
```

**Prompt P0-D (CA root install scripts)**
```
Write deploy/scripts/install-root-ca.ps1 (Windows, run as admin) and deploy/scripts/install-root-ca.sh (macOS).
Each script:
- downloads http://<server>/ca.crt (served by Caddy on port 80, path /ca.crt only);
- verifies the SHA-256 fingerprint passed as a parameter;
- installs the certificate into the machine's trusted root store (Windows: Cert:\LocalMachine\Root; macOS: System keychain, trustRoot).
Print success or failure. Idempotent.
```
