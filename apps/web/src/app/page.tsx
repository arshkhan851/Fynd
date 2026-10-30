import { connection } from "next/server";

import { CompetitionCard } from "@/components/competition-card";
import { SiteHeader } from "@/components/site-header";
import { APP_NAME } from "@/lib/config";
import { getCompetitions } from "@/lib/competitions";
import { checkHealth } from "@/lib/health";

export const metadata = { title: `${APP_NAME} — competition index` };

export default async function Home() {
  await connection();
  const [health, result] = await Promise.all([checkHealth(), getCompetitions()]);
  const competitions = result.status === "ok" ? result.data.items : [];

  return (
    <>
      <SiteHeader />
      <main className="page-shell">
        <section className="home-hero">
          <div className="home-hero__copy">
            <p className="eyebrow">Competition index</p>
            <h1 className="editorial-heading">
              Find your next <em>big challenge.</em>
            </h1>
            <p className="home-hero__intro">
              Explore competitions with reviewed titles, useful dates and links to
              their original sources.
            </p>
          </div>
          <div className="health-note" role="status">
            <span
              aria-hidden="true"
              className={health.status === "ok" ? "health-dot health-dot--ok" : "health-dot"}
            />
            {health.status === "ok"
              ? "API connected"
              : `API unavailable: ${health.message}`}
          </div>
        </section>

        <section
          aria-labelledby="latest-heading"
          className="competition-section"
          id="competitions"
        >
          <div className="section-heading">
            <div>
              <p className="eyebrow">The field</p>
              <h2 className="editorial-heading" id="latest-heading">
                Latest competitions
              </h2>
            </div>
            {result.status === "ok" && (
              <p className="section-count">
                Showing {competitions.length} of {result.data.total}
              </p>
            )}
          </div>

          {result.status === "error" ? (
            <div className="state-panel state-panel--error" role="alert">
              <h3>We couldn’t load competitions</h3>
              <p>{result.message}. Please try again in a little while.</p>
            </div>
          ) : competitions.length === 0 ? (
            <div className="state-panel" role="status">
              <h3>No competitions to show yet</h3>
              <p>Reviewed competition titles will appear here when they are available.</p>
            </div>
          ) : (
            <div className="competition-list">
              <CompetitionCard competition={competitions[0]} featured />
              {competitions.slice(1).map((competition) => (
                <CompetitionCard competition={competition} key={competition.id} />
              ))}
            </div>
          )}
        </section>

        <footer className="page-footer">
          <span>{APP_NAME}</span>
          <span>Competition details link back to their public sources.</span>
        </footer>
      </main>
    </>
  );
}
