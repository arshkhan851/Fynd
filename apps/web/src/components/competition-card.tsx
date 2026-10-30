import Link from "next/link";

import type { Competition } from "@/lib/competitions";

type CompetitionCardProps = {
  competition: Competition;
  featured?: boolean;
};

export function CompetitionCard({
  competition,
  featured = false,
}: CompetitionCardProps) {
  const starts = competition.event_starts_on ?? competition.event_starts_at;
  const deadline =
    competition.registration_deadline_on ?? competition.registration_deadline_at;

  return (
    <article className={featured ? "competition-card competition-card--featured" : "competition-card"}>
      <div className="competition-card__main">
        <p className="eyebrow">{featured ? "In focus" : "Competition"}</p>
        <h3 className={featured ? "competition-card__title competition-card__title--featured" : "competition-card__title"}>
          <Link href={`/competitions/${competition.id}`}>{competition.title}</Link>
        </h3>
        <div className="competition-meta">
          {competition.format && <span>{competition.format}</span>}
          {starts && <span>Starts {starts}</span>}
          {deadline && <span>Registration closes {deadline}</span>}
        </div>
      </div>
      <div className="competition-card__actions">
        {competition.official_url && (
          <a href={competition.official_url} target="_blank" rel="noreferrer">
            Official site <span aria-hidden="true">↗</span>
          </a>
        )}
        <Link href={`/competitions/${competition.id}`}>
          View details <span aria-hidden="true">→</span>
        </Link>
      </div>
    </article>
  );
}
