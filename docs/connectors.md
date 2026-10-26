# Connectors

Sources are added on the **Sources** page (administrators) or with the CLI. Each source has a name, a kind, a
configuration, a sync schedule and a default access list. Use **Test** to check a configuration before the first sync.

| Kind | Reads | Change detection | Permissions |
|---|---|---|---|
| `sharepoint` | SharePoint document libraries (Microsoft Graph) | Graph delta links per drive | Per item, from Graph, plus site groups |
| `onedrive` | A user's OneDrive (Graph) | Graph delta link | Per item, from Graph |
| `smb` | A Windows file share, read directly over SMB 2/3 | Size and modification time | Source default (NTFS ACLs are not mapped) |
| `filesystem` | A folder on the server or a mounted share (NFS, Azure Files) | Size and modification time | Source default |
| `upload` | Files uploaded in the web interface | – | Only the uploader, plus any of the uploader's groups they choose to share with |

Deleting a file at the source removes its document from search at the next sync (control lane, so it is quick).

## SharePoint and OneDrive

### One app registration for sign-in and sync

1. In the Entra admin centre: **App registrations → New registration**, single tenant. Note the *Application (client)
   ID* and *Directory (tenant) ID* → `ENTRA_CLIENT_ID`, `ENTRA_TENANT_ID`.
2. **Authentication → Add a platform → Web**, redirect URI `<PUBLIC_URL>/api/v1/auth/oidc/callback`
   (for example `https://rag.contoso.com/api/v1/auth/oidc/callback`). Only needed for Entra sign-in.
3. **API permissions → Microsoft Graph → Application permissions**, then **Grant admin consent**:
   - `Files.Read.All` and `Sites.Read.All` — read documents and their permissions;
   - `GroupMember.Read.All` — read users' group memberships to resolve access.
   - Delegated `openid`, `profile`, `email`, `offline_access`, `User.Read` — for sign-in.
   
   To limit the app to named sites, grant `Sites.Selected` instead of `Sites.Read.All` and give the app read access to each
   site (Graph `POST /sites/{id}/permissions`).
4. **Certificates & secrets**:
   - A client secret → `ENTRA_CLIENT_SECRET` works for Graph (documents, permissions, group membership).
   - **SharePoint site-group membership uses the SharePoint REST API, which accepts app-only tokens only for
     certificate credentials.** Create a certificate (or upload an existing one), mount its private key (PEM) into the
     `worker` and `backend` containers (see "Mounts and large disks" in docs/scaling.md), and set `ENTRA_CERT_PATH`
     (the path inside the container) and `ENTRA_CERT_THUMBPRINT` (hex SHA-1 thumbprint). The certificate is then used
     for all app-only calls and the secret is not needed. Without a certificate, everything works except resolving
     members of *SharePoint site groups* (SharePoint "Members", "Owners", "Visitors" groups): items shared only
     through those groups show no site-group principals until a certificate is configured.
5. Optional: `ENTRA_ADMIN_GROUP_ID` — members of this security group become administrators when they sign in.

### Adding a SharePoint source

On the Sources page choose **SharePoint** and give:

```json
{
  "site_url": "https://contoso.sharepoint.com/sites/Projects",
  "drives": ["Documents"],
  "field_map": {"doc_number": "DocumentNumber"}
}
```

`drives` limits the libraries (default: all). `field_map` maps SharePoint list columns to document metadata when the
library already has a document register in its columns; those values win over anything guessed from the file.

The first sync lists everything; later syncs fetch only changes. Default schedule: every 60 minutes
(adjustable per source, 5 minutes to 7 days; **Sync now** forces one).

### Adding OneDrive

```json
{"user": "alex@contoso.com"}
```

### What permissions are mapped

Per item the connector reads effective permissions (inherited ones included):

| SharePoint grants | Becomes |
|---|---|
| A user | `user:<Entra object id>` |
| An Entra / Microsoft 365 group | `group:<group id>` |
| A SharePoint site group | `sitegroup:<site id>:<group id>`; members are synced separately |
| "People in your organisation" or anonymous link | `everyone` (every signed-in user) |

A user's own principal set is refreshed from Graph at sign-in and then every 60 minutes (`PRINCIPAL_SYNC_MINUTES`) while they stay active.
Nested group membership is expanded transitively.

## Windows file shares (SMB)

```json
{"server": "fs01.corp.local", "share": "Projects", "path": "Active",
 "username": "svc-rag", "password_env": "SMB_PASSWORD",
 "include": ["**/*"], "exclude": ["**/Archive/**"]}
```

Put `SMB_PASSWORD` in `.env` (it reaches the containers through `env_file`). The account needs read-only access. The
share is read directly, so no mount is required. Documents get the source's default access list — choose the groups
that may see this share when you create the source.

## Server folders and mounted shares

Mount the folder read-only into the `worker` and `backend` containers (examples under "Mounts and large disks" in docs/scaling.md),
then:

```bash
docker compose exec backend rag sources add-folder --name "Projects" --path /sources/projects
```

Default excludes: temporary and lock files (`~$*`), `Thumbs.db`, `.DS_Store`, `.git`, `desktop.ini`.

## Metadata from folders

Folder names tell the system the project and document type. See *Folder rules* in
[configuration.md](configuration.md). Rules apply to every connector, using the path inside the source.

## Supported formats

The readers are dispatched by `ingestion/formats.py` `parse_document`, and the format is detected from the file's first
bytes before its extension is considered.

| Format | Reader |
|---|---|
| PDF (text and scanned) | the `PARSER` role (default `local/pdfium` page router with OCR, tables, drawing title blocks) |
| Word `.docx`, Excel `.xlsx`/`.xlsm`, PowerPoint `.pptx`, CSV/TSV | native readers (no conversion) |
| Outlook `.eml` and `.msg`, HTML, text, Markdown, JSON, XML, logs and other text files | native readers; e-mail attachments become child documents |
| RTF, ODT, Primavera `.xer` | `ingestion/formats_extra.py` |
| Legacy `.doc`, `.xls`, `.ppt`, `.ods`, `.odp` | converted with LibreOffice first: build with `INSTALL_LIBREOFFICE=true` |
| Images (PNG, JPEG, TIFF, BMP, WebP, GIF) | the `OCR` role |
| **AutoCAD `.dwg` and `.dxf`** | `ingestion/cad.py`: DWG is converted to DXF with LibreDWG's `dwg2dxf`. Each sheet (model space and every layout) becomes a page holding its text, dimensions, title-block attributes, layers and blocks. Each sheet is drawn to a PNG for the viewer and, when `VISION` is set, described by the vision model. |
| ZIP | members are ingested individually (limits: 2,000 members, 4 GB, ratio 200). In the upload dialog a ZIP is unpacked first, nested ZIPs included, so files can be selected. |

Revit `.rvt` is not read; it is stored only. Password-protected PDF, Office and ZIP files are detected
(`ingestion/protection.py`), refused with the reason `Protected: …`, and shown with a lock icon. Corrupt and unsupported
files are skipped with the reason shown on the Documents page and in the Monitor page's failed-jobs table.

For connector sources (not for uploads), extensions listed in `SKIP_EXTENSIONS` (default `jpg,jpeg,png`) are recorded as
skipped instead of being read. That saves OCR on photo folders.
