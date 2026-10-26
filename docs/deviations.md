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

1. **Images built and running (updated 2026-10-23).** The Docker images (backend with LibreDWG, frontend, model server)
   have been built, and the development stack (`docker-compose.yml` plus `docker-compose.dev.yml`) runs with real local
   models (`multilingual-e5-small`, `mmarco-mMiniLMv2-L12`). The production compose file with production memory sizes
   has not been run on production hardware.
2. **Paid APIs: OpenRouter only.** The development stack answers through OpenRouter (Claude Haiku / Sonnet), with vision
   through OpenRouter (Gemini Flash Lite with a Claude Haiku fallback) when switched on. Anthropic direct, OpenAI, Azure
   and Cohere are still covered only by contract tests against recorded or simulated responses. Run
   `rag models test LLM_ANSWER` with your key first.
3. **Microsoft 365 connectors were not run against a tenant.** Graph delta, permission mapping and site-group parsing are
   covered by contract tests built from documented response shapes; SharePoint REST group sync requires certificate
   credentials ([connectors.md](connectors.md)).
4. **The default multilingual embedder now runs end to end** (`multilingual-e5-small` with the `mmarco` reranker, measured on
   the 407-question evaluation set in [retrieval.md](retrieval.md)). Arabic retrieval quality still needs a golden set of
   real Arabic questions, and stronger models (bge-m3, bge-reranker-v2-m3) have not been compared
   ([recommendations.md](recommendations.md#32-embeddings-and-reranking-local-no-paid-api)).
4b. **AutoCAD support** (`ingestion/cad.py`, LibreDWG 0.14) is covered by unit tests on generated DXF drawings (`tests/unit/test_cad.py`) and was tried on real DWG
   files. Known limits:
   - a paper-space viewport is drawn empty;
   - hatches are drawn as outlines;
   - external references (xrefs) are not loaded;
   - LibreDWG is GPLv3 (see [recommendations.md §4](recommendations.md#4-security-findings-still-open)).
5. **Nothing was run at scale.** The largest library processed is the 16-document demo set. The 5 TB sizing is
   estimated ([scaling.md](scaling.md)); a 5% pilot should come first.
6. **No external security review.** The checks in [security.md](security.md) are the builder's. Commission a penetration
   test before exposing the system beyond a trusted network.
7. **Docling and MinerU were not run.** Their models come from Hugging Face, which the build sandbox blocked. The parser
   comparison in [parsing.md](parsing.md) covers this system, Tesseract, PyMuPDF4LLM and pdfplumber on five generated
   document kinds; the earlier testing report's Docling and MinerU findings are cited there, not re-measured.
8. **Brand marks** depend on the icon packages available at build time; if one of them changes a file name, the build
   shows a neutral mark for that service rather than failing the build.
