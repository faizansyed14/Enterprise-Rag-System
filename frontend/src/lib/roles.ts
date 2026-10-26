/** Plain names for the model roles (the .env names stay visible next to them for administrators). */
export const ROLE_NAMES: Record<string, string> = {
  LLM_ANSWER: "Answers",
  LLM_ANSWER_HARD: "Hard questions",
  LLM_ANSWER_FALLBACK: "Fallback answers",
  LLM_ROUTER: "Question planning",
  LLM_ENRICH: "Metadata filling",
  VISION: "Drawing and image reading",
  CAD_VISION: "AutoCAD drawing analysis",
  EMBEDDER: "Embeddings",
  RERANKER: "Reranking",
  GROUNDEDNESS: "Answer checking",
  PARSER: "PDF reading",
  OCR: "Scanned pages",
};
