import type { Config } from "tailwindcss";

// Colours are CSS variables (globals.css) so the theme can switch at runtime. color-mix keeps Tailwind's opacity
// modifiers working with them: border-border/70, bg-danger/10 and so on.
const token = (name: string) => `color-mix(in srgb, var(--${name}) calc(<alpha-value> * 100%), transparent)`;

const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: Object.fromEntries(
        [
          "background",
          "foreground",
          "surface",
          "surface-raised",
          "surface-hover",
          "border",
          "border-strong",
          "muted",
          "accent",
          "accent-fg",
          "accent-soft",
          "success",
          "success-soft",
          "danger",
          "danger-soft",
          "warning",
          "warning-soft",
          "sidebar",
          "sidebar-fg",
          "sidebar-muted",
          "sidebar-hover",
          "sidebar-active",
          "sidebar-border",
          "composer",
          "bubble-user",
          "bubble-user-fg",
          "highlight",
        ].map((name) => [name, token(name)]),
      ),
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "Georgia", "serif"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      boxShadow: {
        soft: "var(--shadow-sm)",
        panel: "var(--shadow-md)",
      },
      borderRadius: {
        xl: "var(--radius)",
        "2xl": "var(--radius-lg)",
      },
    },
  },
  plugins: [],
};

export default config;
