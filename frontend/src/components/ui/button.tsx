import clsx from "clsx";
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:opacity-90 disabled:opacity-40",
  secondary: "border border-border-strong bg-transparent hover:bg-surface-hover disabled:opacity-40",
  ghost: "bg-transparent text-muted hover:bg-surface-hover hover:text-foreground disabled:opacity-40",
  danger: "border border-danger/50 text-danger hover:bg-danger-soft disabled:opacity-40",
};

const sizes: Record<Size, string> = {
  sm: "h-8 gap-1.5 px-2.5 text-[13px]",
  md: "h-9 gap-2 px-3.5 text-sm",
};

export function Button({
  variant = "secondary",
  size = "md",
  icon,
  className,
  children,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: Size; icon?: ReactNode }) {
  return (
    <button
      type={type}
      className={clsx(
        "inline-flex shrink-0 items-center justify-center rounded-lg font-medium transition-colors disabled:cursor-not-allowed",
        variants[variant],
        sizes[size],
        className,
      )}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
}

export function IconButton({
  label,
  className,
  children,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      className={clsx(
        "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-foreground disabled:opacity-40",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
