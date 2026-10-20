import type { Metadata, Viewport } from "next";
import "@fontsource-variable/inter/wght.css";
import "@fontsource/cormorant-garamond/latin-500.css";
import "@fontsource/cormorant-garamond/latin-600.css";
import "./globals.css";

import { themeInitScript } from "@/lib/theme";

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME || "RAG";

export const metadata: Metadata = {
  title: { default: APP_NAME, template: `%s – ${APP_NAME}` },
  description: "Answers from your project documents, with the pages they come from.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Theme before first paint (dark by default), so the page never flashes the wrong colours. */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
