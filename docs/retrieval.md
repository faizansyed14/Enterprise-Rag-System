# Retrieval

The aim is simple to state: for a construction question, the passage that answers it should be in the first few
results — and it should come from the right document and the right project.

## Why a construction library is hard

- Questions are often about **identifiers**: `PRJ1-ABC-ZZ-03-DR-S-2001`, `RFI-023`, `03 30 00`, `SUB-084`. Embeddings
  are poor at exact codes; keyword search alone is poor at paraphrase.
- The same document exists in many **copies**. Returning five copies of one specification crowds out the
  evidence.
- Drawings and scans carry their meaning in **title blocks and tables**, not in prose.
- Libraries mix **English and Arabic**, and several projects whose documents look alike.
- Different people may see **different subsets**.

## The pipeline

```
question ─► guardrails ─► router ─► ┌ dense   (embedding similarity)           ┐
                                    ├ sparse  (BM25 over the question words)    ├─► fuse ─► family
                                    ├ sparse  (BM25 over an English rendering)  │   (per    demotion
                                    └ exact   (chunks that contain the identifiers)┘  type)       │
                                                                                                    ▼
                          evidence ◄─ passages ◄─ access re-check ◄─ rerank (cross-encoder) ◄───────┘
```

**1. Router.** Rules classify the question as `identifier`, `keyword`, `question` or `non_english`, pull identifiers
(`A-101`, `RFI 23`, `03 30 00` are normalised to one key),
projects, and decide whether a question is hard enough for the stronger answer model. A fast
LLM (`LLM_ROUTER`) is only used for follow-ups ("what about level 4?") and for rendering a non-English question into
English for the second BM25 leg.

**2. Four legs, one request.** The legs go to Qdrant as a single batch, all with the same filter:

- *dense* — the embedder's vector, with HNSW, or an exact scan when the visible scope has fewer than
  `EXACT_SEARCH_BELOW` chunks (small or tightly filtered scopes get perfect recall);
- *sparse* — BM25 term weights computed by the application (language-aware tokenisation, identifiers kept whole);
- *sparse_en* — the same over the English rendering of a non-English question;
- *exact* — only when the question contains identifiers: chunks whose `identifiers` payload matches, ranked by
  similarity.

**3. The filter is the access control.** Every leg carries a filter built from the user's principals (`everyone`,
`user:<id>`, `group:<id>`, `sitegroup:<site>:<group>`) and any filters from the question.
There is no "search everything then drop what you cannot see" path.

**4. Fusion.** Each leg's scores are max-normalised, then combined with weights that depend on the query type
([configuration.md](configuration.md#fusion-weights)): an identifier question trusts the exact and sparse legs, a
natural-language question still leans on the sparse (word) leg, because near-identical documents are the normal case. The exact leg is lifted into the 0.6–1.0 band, since every hit
contains the identifier. A small boost (0.08) applies when the question names a document type. Max-normalisation
was chosen over reciprocal-rank fusion because it keeps the *margin* between a clear winner and the rest.

**5. Family demotion.** Near-duplicate documents share a `family_id` (MinHash over shingles at ingestion). Within a
family, the representative is the copy that is filed under a project and carries a document number, as long as it
scores at least half of the family's best score; all other members are multiplied by `family_demotion` (0.6) and
marked. Evidence selection later skips demoted near-copies, so five copies of a spec never take five slots.

**Labels.** "Table 11", "Figure 3" and "Appendix 2" in a question are exact lookups like a drawing number: chunks whose own caption or section title carries that label (`TABLE-11`) are fetched by the exact leg. Only captions count, not body sentences that mention a table, so "see Table 3" does not make a chunk look like Table 3.

**5b. Phrase evidence.** The words of the question that stand next to each other, in the same order, in a candidate are
strong evidence that it is the passage meant, and keyword scoring (a bag of words) cannot see it. The share of the question's
word pairs found in a candidate (pairs made only of stop words are ignored) is added to its fused score, times
`PHRASE_BOOST` (0.5; 0.25 was measured slightly worse overall and clearly worse on questions with typos or reworded fragments).

**6. Rerank.** The top `RERANK_TOP_N` (15) candidates go through the cross-encoder, `mmarco-mMiniLMv2-L12` by default (local,
multilingual, so Arabic questions are reranked too). If the question is in a language the reranker does not read, its
English rendering is used when there is one, otherwise the fused order is kept. Scores are scaled to 0..1 within the
window and blended with the fused score. The cross-encoder's share is *adaptive*: up to `RERANK_BLEND` (0.8) when no
candidate quotes the question, falling towards `RERANK_BLEND_FLOOR` (0.2) as a candidate quotes more of it, because a
copied sentence needs no second opinion while a paraphrase does. Questions shorter than `RERANK_MIN_WORDS` and questions
that name an identifier skip it. Scores of (question, chunk) pairs are cached. A reranker outage falls back to the fused
order and never fails the question.

Measured on the 407-question evaluation set (all models local; first-rank page / top-3 page / MRR):

| | no reranker | reranker, adaptive |
|---|---|---|
| all questions | 93.7 / 97.6 / 0.958 | 95.0 / 98.4 / 0.967 |
| paraphrased questions | 73.5 / 91.2 / 0.838 | 82.3 / 100 / 0.907 |
| questions answered by a table | 54.5 / 90.9 / 0.705 | 72.7 / 90.9 / 0.814 |

Fixed shares between 0.3 and 0.8 land within two questions of each other and a window of 40 candidates gains nothing over
15, so those are not worth tuning further on a corpus this size. The English-only `ms-marco-MiniLM-L-6-v2` gains less
(page@1 86.2 -> 87.9 on the 143 hardest questions, against 86.2 -> 90.5 for mmarco).

**What did not help.** Lifting a chunk by its best neighbour on the same page (`PAGE_BOOST`) was expected to improve page
citations and did the opposite: switching it off raised first-rank page accuracy from 89.5 to 93.2 on the evaluation set
(paraphrases 61.8 -> 76.5), because it prefers a page with several mediocre matches over the page with the one chunk that
answers. It is off by default.

**7. Access re-check.** The top candidates' versions are checked against Postgres (`version_access`) with the user's
principals. Qdrant payloads are a performance copy; Postgres is the authority. This closes the gap between a
permission change and the payload update that follows it.

**8. Evidence.** The best `EVIDENCE_PASSAGES` passages are chosen, at most `EVIDENCE_PER_DOCUMENT` per document, each
expanded with its previous and next chunk when that neighbour is on the same page, under the same heading and shorter than 200 tokens, so tables and clauses are not cut in half. Demoted near-copies are skipped, and a candidate below `MIN_EVIDENCE_SCORE` ends the list. Each passage carries the
document, page and the project it belongs to.

## Answers and citations

The answer model sees numbered passages and must cite them. Afterwards the system:

1. parses the citations,
2. finds, for each, the sentence on that page that best supports the claim (ranked by claim coverage, not by
   similarity to a short window),
3. verifies the quoted words occur on the page,
4. checks groundedness of each sentence against its cited passages,
5. marks sentences it could not support, and
6. sets the answer's status:
   - `not_found`: there was no evidence at all (the relevance gate turned the question away, or no passage survived).
     The answer model is then not called.
   - `unverified`: there were citations, but none verified and groundedness was below 0.5. A note is appended.
   - `ok`: otherwise. Only `ok` answers are cached.

**The relevance gate.** This is `retrieval/relevance.py`. A question is turned away only when **every** enabled signal is
weak: best dense cosine, best BM25 score per term, coverage of the question's words, and the cross-encoder's best
probability. By default only the rerank signal is on (`MIN_RERANK=0.02`, English questions only); `MIN_DENSE`,
`MIN_SPARSE` and `MIN_COVERAGE` are 0. Calibrate them with `eval/gate_calibrate.py` on your own unanswerable questions.

**Live Details panel.** Every answer has an info button. It shows each step as it happened:

- the plan;
- the query embedding;
- every search, with each candidate's leg scores and its rerank and final score;
- the files read, and the strategy;
- every model call, with the exact (masked) messages and the reply;
- the evidence check, the arithmetic and the citation checks.

The panel is fed by `monitoring/inspector.py` through SSE `trace` events. It is not stored. `CHAT_DETAILS` controls who
sees it: `all`, or `admin`.

In the chat each source is the file name and page; clicking it (or its `[1]` marker in the answer) opens the original file in a new tab at that page, exactly as uploaded (`/api/v1/documents/<id>/file#page=N`; Office files, which a browser cannot show, download instead). Access is checked on every open.

## What was measured

On the bundled demo library (16 documents in English and Arabic: a specification, an RFI, a site
instruction, a drawing with title block, minutes, a scanned page, a BOQ, a progress report, a second project, a
restricted document and a duplicate), with real ONNX models on the local model server (`all-MiniLM-L6-v2` embedder and
`ms-marco-MiniLM-L-6-v2` reranker) and the offline extractive answerer:

| Metric | Result |
|---|---|
| hit@1, hit@5, MRR | 1.0, 1.0, 1.0 |
| Page hit (the right page retrieved) | 1.0 |
| Not-found questions answered "not found" | 1.0 |
| Facts stated / forbidden facts avoided | 1.0 |
| Median latency on 2 CPUs | ~1.4 s |

The default embedder `multilingual-e5-small` could not be downloaded in the build sandbox (the model host was
unreachable), so these numbers come from `all-MiniLM-L6-v2`, which is English-only; the Arabic question was answered
through the BM25 legs. Run `scripts/eval.sh` after `scripts/models.sh pull` to get the figures for your configuration.

This is a regression suite, not a benchmark: a small library cannot show how retrieval behaves at 5 TB. Build a
golden set from real questions on your own library ([evaluation.md](evaluation.md)) and tune against that.

## Near-duplicate libraries and the fusion weights

An outside test on 30,000 chunks with near-duplicate distractors found the first release's dense-heavy question
weights (0.6 dense / 0.4 sparse) far behind a sparse-heavy setting (hit@20 61% against 87%), with the old repository's
0.3 dense default and 0.15 doing better. That test could not be reproduced here (different data), so the finding was
checked on a purpose-built one: `tests/eval/test_fusion_weights.py` ingests 600 near-identical specification clauses
(20 projects, the same template, different element, level, values and approver) with the real local models and asks 105
questions in three styles. Same index, only the weights differ; each cell is hit@1 / hit@5 / hit@20.

| Weights (question type) | project named | reference code | unique person |
|---|---|---|---|
| dense 0.6 / sparse 0.4 (first release) | 97 / 100 / 100 | **11** / 11 / 11 | 74 / 80 / 80 |
| dense 0.3 / sparse 0.7 | 97 / 100 / 100 | 100 / 100 / 100 | 86 / 100 / 100 |
| dense 0.15 / sparse 0.85 (**new default**) | 97 / 100 / 100 | 100 / 100 / 100 | 100 / 100 / 100 |
| dense 0.1 / sparse 0.9 | 97 / 100 / 100 | 100 / 100 / 100 | 100 / 100 / 100 |

The outside finding holds: with the first release's weights, a question that carries a reference code or a unique name
but no project finds the right clause only 11% of the time. The defaults are now 0.15 / 0.85 for questions and
0.10 / 0.90 for keyword queries.

Limits of this evidence: every question shares exact words with its document, which favours the sparse leg. A library
asked mostly paraphrased questions ("how thick must the cover be" for a clause that says "nominal cover") would pull the
best weights back toward dense. The demo golden set contains such paraphrases and stays at 100%, but it is small. Tune
on your own questions ([evaluation.md](evaluation.md)) and override with `FUSION_WEIGHTS`. The comparison also showed
that naming the project in the question, or filtering on it, removes most of the difficulty (97% at hit@1 for every
weight set), as the outside test found.

## How a question is answered: strategies

After the first search, a small model call (the default answer model) looks at the question and the documents found, and picks
how to gather the evidence (`rag/answering/strategy.py`; rules decide when no model is available):

| Strategy | When | What happens |
|---|---|---|
| lookup | one fact or a short answer | the search's passages; when the question names a file, that file is read too (its beginning plus the parts that match) |
| read_files | the answer needs all of one or several specific documents (every row, a total over a sheet, a whole contract, a comparison of named files) | up to `READ_FILES_MAX_DOCS` (40) documents are read in full within `READ_FILES_BUDGET_CHARS` (400,000). The budget is shared fairly: small files are read whole, and large ones split what is left (`strategy.fair_shares`). For a file that did not fit, its search passages are added, along with up to six of the search's best passages from other files. When the answer model runs on the local server, the limit drops to 3 documents and 60,000 characters. |
| list_all | items spread over many documents (all licences, every invoice from X, which documents mention Y) | up to `AGGREGATE_MAX_DOCS` documents are read and the relevant pages kept (map), then the answer is written from them (reduce) |
| multi_part | several different things, or a comparison | the question is split and each part searched on its own |

Before the answer is written, a second small call checks that the evidence covers every part of the question; when it does not,
the missing part is searched for and many documents are read for it (`SUFFICIENCY_CHECK`). Totals the model writes out but
does not work out are calculated exactly by the program (`rag/answering/arithmetic.py`).
