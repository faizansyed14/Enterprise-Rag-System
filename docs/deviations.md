# Deviations from the design document, and what is not verified

The design document ("Enterprise RAG v2: Final Design and Architecture") was approved before the build. These are the
places where the build differs from it, with the reason, followed by an honest list of what has not been proven.

## Deliberate deviations

| Design document | Build | Reason |
|---|---|---|
| LiteLLM as the provider layer | Two small in-repo adapters: OpenAI-compatible (OpenRouter, OpenAI, Azure OpenAI, any OpenAI-compatible server) and Anthropic native; Cohere and the Azure document services have their own small clients | The LiteLLM package on PyPI was the subject of a supply-chain compromise in March 2026. A multi-provider proxy library holds every provider key by design, so its compromise is severe. Two small adapters are auditable and cover every language-model provider in the design. |
| Agent framework for tool use | An own tool loop (`rag.answering.agent`): research rounds non-streamed with status events, final answer streamed | Fewer dependencies, exact control over limits (`AGENT_MAX_STEPS`, pages read) and over what tools may see (the user's access) |
| Near-duplicate detection | MinHash over shingles, producing a `family_id` per document; the register copy is preferred | Cheap, deterministic, and works across file types; set similarity tolerates partially edited copies better than a single fingerprint |
| PDF reading with parallel workers | One dedicated pdfium thread per process | pdfium is not thread-safe; throughput comes from more processes, not threads |
| Page viewing with a browser PDF library | Server-rendered page images with highlight boxes | Same result for every format (Office, scans, images), no PDF.js bundle or font issues, and highlights are computed where the text positions are known |
| Vendor logos copied into the UI | Marks drawn at build time from open icon packages (Simple Icons, Lobe Icons, Fluent UI branded icons) into a generated file that is not committed | No logo files are redistributed in the repository; marks are trademarks of their owners |
| Hosted observability | Built-in Monitor page, DB-backed; Prometheus/Grafana optional | Requested: no paid observability dependency |
| HHEM groundedness as the default | `builtin/lexical` default; `local/hhem-2.1-open` and LLM judges are selectable | Works with no downloads; the stronger checker needs a model export step (`rag-model-server` supports pair classification) |

Sources for the LiteLLM incident: [Arthur](https://www.arthur.ai/column/litellm-supply-chain-attack-pypi-compromise-2026) and
[Bastion](https://bastion.tech/blog/litellm-pypi-supply-chain-attack).

## What has been verified

- Backend: unit, integration (real Postgres 16 and Qdrant 1.19), contract (cloud providers and Microsoft Graph with
  recorded and simulated responses), model-server and evaluation tests pass.
- Retrieval on the bundled demo library, with real ONNX models (`all-MiniLM-L6-v2`, `ms-marco-MiniLM-L-6-v2`): see
  [retrieval.md](retrieval.md).
- Frontend: typecheck, lint and production build pass; all pages exercised in a real browser against the running
  backend, including streaming answers through the web proxy, the page viewer with highlights, document upload, model
  switching and the Monitor page. Screenshots are in `docs/images`.
- Access control: a restricted user cannot retrieve, cite or open restricted documents (API tests and browser test).
- Compose files validate (`docker compose config`), shell scripts pass `bash -n`.

## What has not been verified

Read this list before relying on the system.

1. **Docker images were not built or run.** The build environment had no reachable Docker daemon or registry. The
   Dockerfiles, compose files and scripts were validated by static checks and by running the same steps by hand (the
   backend and web app were run from source against real Postgres and Qdrant). Expect to fix small things on the first
   `scripts/start.sh`; the logs (`scripts/logs.sh`) will say what.
2. **No call to a paid API was made.** OpenRouter, Anthropic, OpenAI, Azure and Cohere adapters are covered by contract
   tests against recorded or simulated responses. The demo used the offline extractive answerer. Run
   `rag models test LLM_ANSWER` with your key first.
3. **Microsoft 365 connectors were not run against a tenant.** Graph delta, permission mapping and site-group parsing are
   covered by contract tests built from documented response shapes; SharePoint REST group sync requires certificate
   credentials ([connectors.md](connectors.md)).
4. **The default multilingual embedder was not run end to end.** `multilingual-e5-small` could not be downloaded in the
   sandbox; the English `all-MiniLM-L6-v2` was used for the measured results. The code path is the same (the model
   server loads either), but Arabic retrieval quality with e5 or bge-m3 needs your own evaluation.
5. **Nothing was run at scale.** The largest library processed is the 16-document demo set. The 5 TB sizing is
   estimated ([scaling.md](scaling.md)); a 5% pilot should come first.
6. **No external security review.** The checks in [security.md](security.md) are the builder's. Commission a penetration
   test before exposing the system beyond a trusted network.
7. **Docling and MinerU were not run.** Their models come from Hugging Face, which the build sandbox blocked. The parser
   comparison in [parsing.md](parsing.md) covers this system, Tesseract, PyMuPDF4LLM and pdfplumber on five generated
   document kinds; the earlier testing report's Docling and MinerU findings are cited there, not re-measured.
8. **Brand marks** depend on the icon packages available at build time; if one of them changes a file name, the build
   shows a neutral mark for that service rather than failing the build.
