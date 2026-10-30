import Link from "next/link";

import { SiteHeader } from "@/components/site-header";

export default function CompetitionNotFound() {
  return (
    <>
      <SiteHeader />
      <main className="page-shell detail-page">
        <div className="state-panel" role="status">
          <p className="eyebrow">Not found</p>
          <h1 className="editorial-heading">This competition isn’t available.</h1>
          <p>It may not have a reviewed title or may have been removed.</p>
          <Link className="text-link" href="/#competitions">
            Return to competitions <span aria-hidden="true">→</span>
          </Link>
        </div>
      </main>
    </>
  );
}
