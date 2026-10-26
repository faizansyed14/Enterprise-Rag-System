# Evaluation

Retrieval quality is only real when measured on your own questions. The system ships a small evaluation runner so any
change — a different embedder, new fusion weights, a new chunk size — can be judged by numbers rather than by feel.

## Golden sets

A golden set is a YAML file in the evaluation folder (`/data/eval` in the containers, `EVAL_DIR` from source). The file
name is the set name: `/data/eval/site-a.yaml` is run as `scripts/eval.sh site-a`. The built-in `demo` set matches the
demo library.

```yaml
- id: column-grade
  question: What concrete grade is required for the columns on Level 3?
  expect:
    documents: ["03 30 00"]        # document numbers (any spelling) or parts of file names
    pages: [1]                     # optional: a page that must be retrieved
    answer_contains: ["C40"]       # facts the answer must state
    answer_not_contains: ["C35"]   # facts it must not state (wrong values)
    not_found: false               # true when the library does not answer the question
  principals: ["everyone"]         # optional: whose access to search with

- id: restricted-variation
  question: What is the agreed value of variation order VO-15?
  principals: ["everyone"]
  expect:
    not_found: true                # a user without access must get "not found", not the value
    answer_not_contains: ["1,450,000"]
```

Put the file in the `data` volume:

```bash
docker compose cp site-a.yaml backend:/data/eval/site-a.yaml
scripts/eval.sh site-a
```

or use the Evaluation page, which lists sets and runs them.

## Metrics

| Metric | Meaning |
|---|---|
| `hit@1`, `hit@3`, `hit@5`, `hit@10` | The expected document is within the first k documents. A document of another project, when `project` is given, does not count |
| `mrr` | Mean reciprocal rank of the first expected document |
| `page_hit@5` | The expected page is among the first five passages |
| `facts` | Share of `answer_contains` values the answer states |
| `no_wrong_facts` | None of the `answer_not_contains` values appear |
| `not_found_correct` | Questions marked `not_found` were answered "not found", and the others were not |
| `latency_p50_ms`, `latency_p95_ms` | Median and 95th percentile of the whole answer |

Answer-quality metrics also include verified citations and groundedness when an answer is generated.

`--no-answer` measures retrieval only (no model calls, no cost). The CLI accepts `--min-hit5` and exits non-zero below
it, which makes the set usable as a CI gate:

```bash
docker compose exec -T backend rag eval run --set site-a --no-answer --min-hit5 0.9 --output /data/eval/last-run.json
```

## Building a good set

1. Collect 50–200 real questions from the people who will use the system, across projects, document types and both
   languages.
2. For each, record the document and page a correct answer must come from, and any values that distinguish the current
   revision from older ones.
3. Include questions the library **cannot** answer, questions about superseded revisions, and questions where a user
   without access must get nothing. These catch the failures that matter most.
4. Include identifier-only queries ("RFI-023", "A-101 rev B") and paraphrased ones; they stress different legs.
5. Run before and after every change to models, weights or chunking; keep the JSON outputs.

## Tests

`tests/eval/test_golden_sets.py` runs the demo set twice: with the offline test models (hashing embedder, lexical
reranker, extractive answers), which proves the pipeline structure — identifiers, BM25, fusion, revisions, access
control, families — and, when `RAG_TEST_MODELS_DIR` points at downloaded ONNX models, with the real local models. Both
fail the build if retrieval drops below the set thresholds. The remaining automated tests are described in the README (unit, integration against real Postgres and
Qdrant, contract tests for cloud providers and Microsoft Graph with recorded responses, model server).
