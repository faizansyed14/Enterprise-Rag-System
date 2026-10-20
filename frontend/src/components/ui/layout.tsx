import clsx from "clsx";
import type { ReactNode } from "react";

/** Page heading: the serif title is the one decorative element on admin pages. */
export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border px-6 pb-5 pt-7 lg:px-10">
      <div className="min-w-0">
        <h1 className="font-display text-[34px] font-medium leading-none tracking-[-0.01em]">{title}</h1>
        {description ? <p className="mt-2 max-w-2xl text-[13.5px] text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Section({ title, description, actions, children, className }: { title: string; description?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={clsx("rounded-2xl border border-border bg-surface-raised", className)}>
      <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-3">
        <div className="min-w-0">
          <h2 className="text-[13.5px] font-semibold">{title}</h2>
          {description ? <p className="mt-0.5 text-[12.5px] text-muted">{description}</p> : null}
        </div>
        {actions ? <div className="flex items-center gap-1.5">{actions}</div> : null}
      </div>
      <div>{children}</div>
    </section>
  );
}

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className={clsx("w-full border-collapse text-left text-[13px]", className)}>{children}</table>
    </div>
  );
}

export function Th({ children, className, align = "left" }: { children?: ReactNode; className?: string; align?: "left" | "right" }) {
  return (
    <th className={clsx("whitespace-nowrap border-b border-border px-4 py-2 text-[12px] font-medium text-muted", align === "right" && "text-right", className)}>
      {children}
    </th>
  );
}

export function Td({
  children,
  className,
  align = "left",
  title,
  colSpan,
}: {
  children?: ReactNode;
  className?: string;
  align?: "left" | "right";
  title?: string;
  colSpan?: number;
}) {
  return (
    <td title={title} colSpan={colSpan} className={clsx("border-b border-border/70 px-4 py-2.5 align-middle", align === "right" && "text-right tabular", className)}>
      {children}
    </td>
  );
}

export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (v: T) => void; items: { value: T; label: ReactNode }[] }) {
  return (
    <div role="tablist" className="inline-flex rounded-lg border border-border p-0.5">
      {items.map((item) => (
        <button
          key={item.value}
          role="tab"
          type="button"
          aria-selected={value === item.value}
          onClick={() => onChange(item.value)}
          className={clsx(
            "h-7 rounded-md px-2.5 text-[12.5px] font-medium transition-colors",
            value === item.value ? "bg-surface-hover text-foreground" : "text-muted hover:text-foreground",
          )}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

export function Stat({ label, value, detail, children }: { label: string; value: ReactNode; detail?: ReactNode; children?: ReactNode }) {
  return (
    <div className="min-w-0 rounded-2xl border border-border bg-surface-raised px-5 py-4">
      <div className="text-[12.5px] text-muted">{label}</div>
      <div className="mt-1.5 text-[28px] font-semibold leading-none tracking-[-0.02em] tabular">{value}</div>
      {detail ? <div className="mt-2 text-[12.5px] text-muted">{detail}</div> : null}
      {children ? <div className="mt-3">{children}</div> : null}
    </div>
  );
}

/** Empty cells that complete the last row of a hairline grid (gap-px on a border-coloured background), so no
 *  grey block shows where a row ends early. `layout` names the column counts at the sm/md and xl breakpoints. */
export function GridFillers({ count, layout }: { count: number; layout: "2-3" | "2" | "2-4" }) {
  const cells: string[] = [];
  if (layout === "2-3") {
    if (count % 2) cells.push("hidden sm:block xl:hidden");
    for (let i = 0; i < (3 - (count % 3)) % 3; i++) cells.push("hidden xl:block");
  } else if (layout === "2") {
    if (count % 2) cells.push("hidden sm:block");
  } else {
    if (count % 2) cells.push("hidden md:block xl:hidden");
    for (let i = 0; i < (4 - (count % 4)) % 4; i++) cells.push("hidden xl:block");
  }
  return (
    <>
      {cells.map((c, i) => (
        <div key={i} aria-hidden className={clsx("bg-surface-raised", c)} />
      ))}
    </>
  );
}
