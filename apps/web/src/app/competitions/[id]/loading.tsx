import { SiteHeader } from "@/components/site-header";

export default function LoadingCompetition() {
  return (
    <>
      <SiteHeader />
      <main className="page-shell detail-page">
        <p aria-live="polite" className="eyebrow">
          Loading competition details…
        </p>
        <div aria-hidden="true" className="detail-loading-block" />
      </main>
    </>
  );
}
