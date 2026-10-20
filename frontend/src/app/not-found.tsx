import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex min-h-full flex-col items-center justify-center px-6 text-center">
      <p className="stamp text-muted">Sheet missing</p>
      <h1 className="mt-4 font-display text-[40px] font-medium leading-tight">This page is not in the set</h1>
      <p className="mt-2 max-w-md text-[14px] text-muted">The address may be mistyped, or the page was moved. Documents you cannot open are not shown either.</p>
      <Link href="/" className="mt-6 inline-flex h-9 items-center rounded-lg bg-accent px-4 text-sm font-medium text-accent-fg">
        Back to questions
      </Link>
    </main>
  );
}
