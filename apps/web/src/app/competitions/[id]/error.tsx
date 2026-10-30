"use client";

import { SiteHeader } from "@/components/site-header";

export default function CompetitionError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <>
      <SiteHeader />
      <main className="page-shell detail-page">
        <div className="state-panel state-panel--error" role="alert">
          <p className="eyebrow">Something went wrong</p>
          <h1 className="editorial-heading">Details are temporarily unavailable.</h1>
          <p>Please try loading this page again.</p>
          <button className="button-link" onClick={reset} type="button">
            Try again
          </button>
        </div>
      </main>
    </>
  );
}
