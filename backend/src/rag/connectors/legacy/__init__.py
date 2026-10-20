"""Connectors brought over from the previous code base, nearly unchanged (imports only), with their tests.

They speak the old contract (a synchronous `scan(cursor)` of `ScanBatch`es, `open(file)`, `refresh_acl(file)`);
`rag.connectors.legacy_adapter` presents each one as a normal connector of this system. Credentials are never stored in the
database: a source's config names the environment variable that holds each secret (`token_env`, `password_env`,
`client_secret_env`, `access_key_env` ...), which must be set on the workers.

Kinds this system already has natively (sharepoint, onedrive, smb, filesystem, upload) are not taken from here.
"""

from __future__ import annotations

# kind -> "module:Class" (imported lazily, so a missing optional package only affects the connector that needs it)
LEGACY_CONNECTORS: dict[str, str] = {
    # files and storage
    "s3": "s3:S3Connector",                       # AWS S3, Cloudflare R2, MinIO, Wasabi, B2, Spaces, OCI, Ceph (pip install boto3)
    "azure_blob": "azure_blob:AzureBlobConnector",
    "gcs": "gcs:GcsConnector",
    "webdav": "webdav:WebDavConnector",           # Nextcloud, ownCloud, Apache, on-prem DAV
    "ftp": "ftp:FtpConnector",
    "sftp": "sftp:SftpConnector",                 # pip install paramiko
    "gdrive": "gdrive:GDriveConnector",
    "dropbox": "dropbox:DropboxConnector",
    "box": "box:BoxConnector",
    # knowledge, tickets, code, chat, mail, data
    "confluence": "confluence:ConfluenceConnector",
    "jira": "jira:JiraConnector",
    "notion": "notion:NotionConnector",
    "slack": "slack:SlackConnector",
    "discord": "discord:DiscordConnector",
    "teams": "graph_mail_teams:TeamsConnector",
    "outlook_mail": "graph_mail_teams:OutlookMailConnector",
    "github": "github:GitHubConnector",
    "gitlab": "gitlab:GitLabConnector",
    "bitbucket": "bitbucket:BitbucketConnector",
    "zendesk": "zendesk:ZendeskConnector",
    "asana": "asana:AsanaConnector",
    "airtable": "airtable:AirtableConnector",
    "gmail": "gmail:GmailConnector",
    "imap": "imap:ImapConnector",
    "sql": "sql:SqlConnector",
    "web": "web:WebConnector",                    # sites, sitemaps, RSS / Atom
}
