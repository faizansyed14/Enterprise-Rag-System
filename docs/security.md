# Security

## Model of access

Every document version has an access list of **principals**: `everyone`, `user:<id>`, `group:<id>`,
`sitegroup:<site>:<group>`. Every user has a set of principals (their own, their Entra groups and the SharePoint site
groups they belong to, plus `everyone`). A document is visible when the two sets intersect.

- For SharePoint and OneDrive, access is read per item from Microsoft Graph (effective permissions, inherited ones
  included, sharing links mapped to `everyone` or the listed people) and refreshed by the delta sync.
- For folders, SMB shares and uploads, the source's default access list applies (a source can be limited to named
  groups). NTFS ACLs are not mapped.
- Access is enforced in the vector search filter, so unauthorised text never leaves the index, and re-checked against
  Postgres before anything is shown ([retrieval.md](retrieval.md)).
- Permission changes and deletions run on the **control** lane with a dedicated worker. An alert fires if one waits
  more than five minutes.
- Search is **fail-closed**: the filter requires a match, so a version with an empty access list matches no principal
  and is visible to nobody.
- The page-image, document, citation and conversation endpoints each apply the same check. A user cannot read another
  user's conversation.

## Authentication and sessions

- Local accounts: Argon2id hashes, 8 failed attempts lock the account for 15 minutes, and sign-in rate limits count
  only failures per client address (`LOGIN_ATTEMPTS_PER_15MIN`). Wrong password, unknown user and locked account give
  the same answer.
- Microsoft Entra ID: OpenID Connect authorisation-code flow with PKCE, state and nonce; members of
  `ENTRA_ADMIN_GROUP_ID` become administrators. `AUTH_MODE=both` allows either.
- Sessions: random token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` in production); only the hash is stored.
  Absolute lifetime `SESSION_TTL_HOURS`, idle timeout `SESSION_IDLE_MINUTES`.
- CSRF: every state-changing request must echo the session's token in `X-CSRF-Token`; the origin must be in
  `ALLOWED_ORIGINS`.
- Roles: administrator and ordinary user. Administrator pages (Models, Sources, Monitor, Lab, Evaluation, Users) are
  enforced by the API, not only hidden in the UI.

## Prompt injection and unsafe content

Retrieved text is untrusted. Markup that imitates the prompt's own delimiters is defused in the evidence, the evidence
is delimited in the prompt, and the system prompt tells the model to treat it as data and never as instructions. Agent tools are read-only and run inside the user's
access, so a poisoned document cannot cause actions or reach documents the user cannot see. Questions are cleaned of control characters, capped in length, and refused when they match two or more
known instruction-override phrases; a single match is only flagged. Citations are verified against the page text, so a fabricated source does not display as
verified.

## Uploads

Type is detected from content, not the name; the extension must agree. Office files are checked for zip bombs
(member count, expanded size, compression ratio); file names are normalised; size limits apply (`UPLOAD_MAX_MB` for the
web, `MAX_FILE_MB` for sources). With `CLAMAV_URL` set, files are scanned before they are stored (see
[commands.md](../commands.md)). Originals are never served inline from the original host: they download with
`Content-Disposition: attachment`, and page images are rendered by the server.

## Bank accounts, cards and ids with outside models

What stays on your servers: parsing, OCR (the local one), embeddings, the reranker, the database and the search index, including
every value in a document. What can leave: the text of the retrieved passages and the question (answers), the first ~6,000
characters of a document (metadata filling), the question and history (question planning), and page images (the vision model, when
switched on). Documents or sources marked `local_only` never go to a cloud model at all.

For the values below the system adds a shield at the one place where requests to cloud models are built (`rag/privacy/shield.py`):

| Value | Found by |
|---|---|
| IBAN | the format, and the mod-97 check digit, so a number with a wrong check digit is not mistaken for one |
| Card number | 13-19 digits that pass the Luhn check |
| Bank account number, sort code, routing / ABA / IFSC / BSB | a label next to a number of at least 6 digits ("Account No: ...", "Beneficiary account", "Sort code") |
| SWIFT / BIC | a label next to the code |
| US social security number, Emirates ID, passport number | the format (passport: with its label) |
| E-mail address | the format |
| Phone number | international form (`+971 ...`), a label ("Tel", "Mobile", "WhatsApp", "Fax"), or the UAE shapes `05x xxx xxxx` and `0x xxx xxxx`; dates, amounts, order and page numbers are left alone |

- **Masking.** Before a request leaves, each value becomes a placeholder (`[[ACCOUNT_1]]`) and, in the reply, the placeholder is put
  back. The model never sees the number, and the person still reads it ("the account number is 1234..."). The same value gets the
  same placeholder within one request, including in the arguments of search calls the model makes. Streamed answers are
  restored piece by piece, never showing half a placeholder.
- **Which values hold something back.** E-mail and phone numbers are masked in text like the rest, but only bank and id values
  (accounts, IBANs, cards, SWIFT, national ids, passports) keep a scanned page away from the vision model or, with
  `PRIVACY_AUTO_LOCAL_ONLY`, a whole document away from cloud models: nearly every letterhead has a phone number, so counting those would
  send nothing to vision and hold back nearly every document.
- **Images.** An image cannot be masked. A scanned page is read by the local OCR first; if the text holds one of these values,
  the page is not sent to the vision model and the local reading is used. A drawing sheet whose text layer holds one is not sent
  either.
- **Hard removal.** `PRIVACY_REDACT_STORED=true` removes the values from the stored text of files read from then on
  (`[redacted account]`); they can then not be found, shown or cited. Use it when the values must not be in the index at all.
- **What you see in the app.** While a file is being indexed, the upload list shows a shield line as soon as it has been read (for example
  "Found 1 bank account number and 1 IBAN: replaced by placeholders before any cloud model sees them"). Each document page shows the same in a
  banner, with a **What a model receives** button that opens the masked passages page by page. The page itself is your stored copy and
  keeps the real values.
- **How to check it.** The Documents view shows the stored text from your own database, with the real numbers; nothing is sent to
  show it. `rag privacy check "<file name>"` prints what a cloud model would receive from a document (the placeholders), and
  `rag privacy calls` lists recent cloud model calls with the number of values masked in each. Every call is also stored with that
  count (`spans.attrs.masked_values`; empty means no shield on that call, e.g. a local model).
- **Whole-document hold-back.** A bank statement carries more than an account number: counterparty names, references, balances. No pattern
  can mark those, so for such files masking is partial. `PRIVACY_AUTO_LOCAL_ONLY=true` treats any document that holds a bank account, IBAN,
  card or id number as `local_only` by itself: no passage from it is sent to a cloud model (not even masked), and it skips metadata
  filling and cloud vision. It can still be searched, and answered with the local answer option. Documents already stored need
  `rag reprocess`.
- **Limits.** Detection is by pattern and label, so an unlabelled account number in prose ("transfer 8812345678 to Ali") or in an
  image the local OCR cannot read is not recognised. Names and addresses are not masked.
  Documents that must never reach any outside model should be marked `local_only`, or the answer models set to the local option.

## Secrets and data handling

- Secrets live in `.env` only; `scripts/start.sh` generates `SECRET_KEY`, `POSTGRES_PASSWORD`, `QDRANT_API_KEY`,
  `LOCAL_MODELS_TOKEN`, the bootstrap admin password and the Grafana password. Compose refuses to start without the
  database and Qdrant secrets.
- The SMB password is never stored in the database; the source names an environment variable.
- Log lines have e-mail addresses, IBANs, card numbers and phone numbers masked when `MASK_PII_IN_LOGS=true`. Prompts and answers are
  not written to logs; traces keep timings, model names, token counts and hashes.
- `OPENROUTER_ZDR=true` restricts routing to providers that do not retain prompts. For a stricter stance use a model
  provider contract that fits your policy (Azure OpenAI in your tenant, or a self-hosted OpenAI-compatible server).
- Audit log (`audit_log` table): sign-ins and failed sign-ins, sign-outs, uploads, downloads, source changes and syncs,
  model switches, index rebuild/activate/rollback, job retries and cancellations, user changes and evaluation runs are
  written with actor and address.

## Network and containers

- Only nginx publishes a port. Postgres, Qdrant, the model server, the API and the web server are on an internal network.
  Prometheus and Grafana, when enabled, bind to `127.0.0.1`.
- nginx sets `X-Content-Type-Options`, `X-Frame-Options: DENY`, a referrer policy, and blocks `/metrics`
  from outside; the API adds a restrictive Content-Security-Policy to its JSON responses.
- Containers run as non-root (uid 10001 for the backend, `node` for the frontend), with `init` and bounded logs.
- The API trusts `X-Forwarded-For` only because only nginx and the web server can reach it; if you put another proxy in front of nginx,
  keep it that way.

## Supply chain

The backend does not use LiteLLM or any multi-provider proxy library: provider calls go through two small adapters in
this repository (OpenAI-compatible and Anthropic). That choice was made after the March 2026 compromise of the LiteLLM
PyPI package; see [deviations.md](deviations.md) for the sources. Python dependencies are locked with hashes (`uv.lock`), JavaScript
with `package-lock.json`. Run `uv run pip-audit`, `npm audit --omit=dev` and `bandit` (included in `scripts/test.sh`)
before each release, and pin image tags to digests in your registry.

Audit status at release: `pip-audit` and `npm audit --omit=dev` (everything that ships in the runtime images) report no
known vulnerabilities. `npm audit` including development tools reports 9 advisories (7 high) in build-time packages
(`braces`, `micromatch`, `fast-glob` and `postcss-selector-parser`, pulled in by Tailwind CSS 3 and the ESLint
configuration). They process only this repository's own source files during the build and are not present in the
runtime image; moving to Tailwind CSS 4 removes them and is a planned, separate change because it alters the styling
pipeline.

## Hardening checklist before go-live

- [ ] `APP_ENV=prod`, a long random `SECRET_KEY`, HTTPS (terminate TLS on a load balancer or add a TLS server block to `deploy/nginx.conf`).
- [ ] Bootstrap admin password changed and `BOOTSTRAP_ADMIN_PASSWORD` cleared from `.env`.
- [ ] Entra sign-in enabled (`AUTH_MODE=entra`) with conditional access applied in Entra.
- [ ] `ALLOWED_ORIGINS` and `PUBLIC_URL` set to the real host only.
- [ ] Disk encryption for the Postgres, Qdrant and originals volumes; encrypted, access-controlled backups
      (`backups/` contains `.env` and all documents).
- [ ] ClamAV profile on if users upload files.
- [ ] Alert webhook configured; someone owns it.
- [ ] Firewall: only 80/443 reachable; Grafana through a VPN or SSH tunnel only.
- [ ] Access test: sign in as a restricted user and confirm a restricted document returns "not found"
      (the demo golden set includes this check).
- [ ] Dependency audits clean or understood; image tags pinned.
- [ ] Penetration test of the deployed system by someone who did not build it.
