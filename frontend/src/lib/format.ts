export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} d ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)} s`;
}

export function compact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value > 0 && value < 0.01) return `$${value.toFixed(4)}`;
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(value);
}

export function pct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

const DOC_TYPE_LABELS: Record<string, string> = {
  drawing: "Drawing",
  specification: "Specification",
  rfi: "RFI",
  submittal: "Submittal",
  minutes: "Minutes",
  report: "Report",
  change_order: "Change order",
  invoice: "Invoice",
  contract: "Contract",
  correspondence: "Correspondence",
  transmittal: "Transmittal",
  schedule: "Schedule",
  boq: "Bill of quantities",
  calculation: "Calculation",
  method_statement: "Method statement",
  inspection: "Inspection",
  site_instruction: "Site instruction",
  model: "Model",
  survey: "Survey",
  photo: "Photo",
};

export function docTypeLabel(value: string | null | undefined): string {
  if (!value) return "Document";
  return DOC_TYPE_LABELS[value] ?? value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function titleCase(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
