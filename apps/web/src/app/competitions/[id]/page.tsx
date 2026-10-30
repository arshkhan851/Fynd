import Link from "next/link";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import { cache } from "react";

import { SiteHeader } from "@/components/site-header";
import { APP_NAME } from "@/lib/config";
import { getCompetition } from "@/lib/competitions";

type CompetitionDetailPageProps = {
  params: Promise<{ id: string }>;
};

const loadCompetition = cache((id: string) => getCompetition(id));

export async function generateMetadata({ params }: CompetitionDetailPageProps) {
  const { id } = await params;
  const result = await loadCompetition(id);
  return {
    title:
      result.status === "ok"
        ? `${result.data.title} — ${APP_NAME}`
        : `Competition details — ${APP_NAME}`,
  };
}

function MetadataItem({ label, value }: { label: string; value: string | null }) {
  return (
    <li className="metadata-item">
      <span className="eyebrow">{label}</span>
      <span>{value ?? "Not provided"}</span>
    </li>
  );
}

function ExternalLink({ href, children }: { href: string; children: string }) {
  return (
    <a className="editorial-link" href={href} target="_blank" rel="noreferrer">
      {children} <span aria-hidden="true">↗</span>
    </a>
  );
}

export default async function CompetitionDetailPage({
  params,
}: CompetitionDetailPageProps) {
  await connection();
  const { id } = await params;
  const result = await loadCompetition(id);
  if (result.status === "not-found") notFound();

  if (result.status === "error") {
    return (
      <>
        <SiteHeader />
        <main className="page-shell detail-page">
          <Link className="back-link" href="/#competitions">
            ← All competitions
          </Link>
          <div className="state-panel state-panel--error" role="alert">
            <h1>Details are unavailable</h1>
            <p>We couldn’t load this competition. Please try again later.</p>
          </div>
        </main>
      </>
    );
  }

  const competition = result.data;
  const starts = competition.event_starts_on ?? competition.event_starts_at;
  const ends = competition.event_ends_on ?? competition.event_ends_at;
  const registrationOpens =
    competition.registration_opens_on ?? competition.registration_opens_at;
  const registrationDeadline =
    competition.registration_deadline_on ??
    competition.registration_deadline_at;

  return (
    <>
      <SiteHeader />
      <main className="page-shell detail-page">
        <Link className="back-link" href="/#competitions">
          ← All competitions
        </Link>

        <section className="detail-hero">
          <p className="eyebrow">
            {competition.format ?? "Competition details"}
          </p>
          <h1 className="editorial-heading">{competition.title}</h1>
          {competition.categories.length > 0 && (
            <ul aria-label="Categories" className="tag-list">
              {competition.categories.map((category) => (
                <li className="tag" key={category.slug}>
                  {category.name}
                  {category.is_primary && <span className="sr-only"> (primary)</span>}
                </li>
              ))}
            </ul>
          )}
        </section>

        <ul aria-label="Competition metadata" className="metadata-strip">
          <MetadataItem label="Format" value={competition.format} />
          <MetadataItem label="Starts" value={starts} />
          <MetadataItem label="Ends" value={ends} />
          <MetadataItem label="Registration opens" value={registrationOpens} />
          <MetadataItem label="Registration deadline" value={registrationDeadline} />
          <MetadataItem label="Timezone" value={competition.event_timezone} />
        </ul>

        <div className="detail-grid">
          <div className="detail-main-column">
            <section className="detail-section">
              <p className="eyebrow">The overview</p>
              <h2 className="editorial-heading">Overview</h2>
              <p className="detail-copy">
                {competition.description ?? "Overview information is not available yet."}
              </p>
            </section>
            <section className="detail-section">
              <p className="eyebrow">Eligibility</p>
              <h2 className="editorial-heading">Who can take part</h2>
              <p className="detail-copy">
                {competition.eligibility_summary ??
                  "Eligibility information is not available yet."}
              </p>
            </section>
            <section className="detail-section">
              <p className="eyebrow">Challenge</p>
              <h2 className="editorial-heading">The challenge</h2>
              <p className="detail-copy detail-copy--muted">
                Challenge details have not been provided.
              </p>
            </section>
          </div>

          <aside className="detail-aside">
            <section className="aside-section">
              <p className="eyebrow">Continue exploring</p>
              <h2>Important links</h2>
              <ul className="link-list">
                {competition.official_url && (
                  <li>
                    <ExternalLink href={competition.official_url}>Official website</ExternalLink>
                  </li>
                )}
                {competition.registration_url && (
                  <li>
                    <ExternalLink href={competition.registration_url}>
                      Registration
                    </ExternalLink>
                  </li>
                )}
                {competition.sources.map((source, index) => (
                  <li key={`${source.source_url}-${index}`}>
                    <ExternalLink href={source.source_url}>
                      {source.publisher_name ?? "Competition source"}
                    </ExternalLink>
                  </li>
                ))}
                {!competition.official_url &&
                  !competition.registration_url &&
                  competition.sources.length === 0 && (
                    <li className="detail-copy--muted">No links are available yet.</li>
                  )}
              </ul>
            </section>

            <section className="aside-section">
              <p className="eyebrow">Organized by</p>
              <h2>Organizers</h2>
              {competition.organizers.length > 0 ? (
                <ul className="plain-list">
                  {competition.organizers.map((organizer) => (
                    <li key={`${organizer.name}-${organizer.role ?? ""}`}>
                      {organizer.website_url ? (
                        <ExternalLink href={organizer.website_url}>{organizer.name}</ExternalLink>
                      ) : (
                        organizer.name
                      )}
                      {organizer.role && <span className="list-note">{organizer.role}</span>}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-copy--muted">Organizer information is not available.</p>
              )}
            </section>

            <section className="aside-section">
              <p className="eyebrow">Awards</p>
              <h2>Prizes</h2>
              {competition.prizes.length > 0 ? (
                <ul className="plain-list">
                  {competition.prizes.map((prize, index) => (
                    <li key={`${prize.place ?? ""}-${prize.award_name ?? index}`}>
                      <span>{prize.place ?? prize.award_name ?? "Prize"}</span>
                      {prize.amount && (
                        <span className="list-note">
                          {prize.currency ? `${prize.currency} ` : ""}{prize.amount}
                        </span>
                      )}
                      {prize.details && <span className="list-note">{prize.details}</span>}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-copy--muted">Prize information is not available.</p>
              )}
            </section>
          </aside>
        </div>
      </main>
    </>
  );
}
