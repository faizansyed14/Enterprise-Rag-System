"use client";

import { RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/feedback";

export default function PageError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <EmptyState
      title="This page stopped working"
      action={
        <Button onClick={reset} icon={<RotateCcw size={14} aria-hidden />}>
          Try again
        </Button>
      }
    >
      Something in the page failed to draw. Trying again usually works; if not, reload the browser tab.
      {error.digest ? <span className="mt-2 block text-[12px]">Reference {error.digest}</span> : null}
    </EmptyState>
  );
}
