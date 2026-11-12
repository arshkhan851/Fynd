import Link from "next/link";
import { connection } from "next/server";

import { CompetitionCard } from "@/components/competition-card";
import { SiteHeader } from "@/components/site-header";
import { APP_NAME } from "@/lib/config";
import { getCompetitionCategories, getCompetitions } from "@/lib/competitions";
import { checkHealth } from "@/lib/health";

export const metadata = { title: `${APP_NAME} — competition index` };

type HomeProps = {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
};

function singleValue(value: string | string[] | undefined): string {
  return typeof value === "string" ? value : "";
}

export default async function Home({ searchParams }: HomeProps) {
  await connection();
  const params = await searchParams;
  const q = singleValue(params.q).trim();
  const category = singleValue(params.category);
  const requestedOffset = Number.parseInt(singleValue(params.offset) || "0", 10);
  const offset = Number.isSafeInteger(requestedOffset) && requestedOffset > 0
    ? requestedOffset
    : 0;
  const [health, result, categories] = await Promise.all([
    checkHealth(),
    getCompetitions(undefined, undefined, { q, category, offset }),
    getCompetitionCategories(),
  ]);
  const page = result.status === "ok" ? result.data : null;
  const competitions = page?.items ?? [];
  const hasFilters = Boolean(q || category);
  const previousOffset = Math.max(0, offset - (page?.limit ?? 20));
  const nextOffset = offset + (page?.limit ?? 20);
  const queryForOffset = (next: number) => {
    const query = new URLSearchParams();
    if (q) query.set("q", q);
    if (category) query.set("category", category);
    if (next > 0) query.set("offset", String(next));
    return `/?${query.toString()}#competitions`;
  };

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
                {hasFilters ? "Competition results" : "Latest competitions"}
              </h2>
            </div>
            {page && (
              <p className="section-count" aria-live="polite">
                {page.total === 0
                  ? "No matching competitions"
                  : offset >= page.total
                    ? "No competitions on this page"
                    : `Showing ${offset + 1}–${Math.min(offset + competitions.length, page.total)} of ${page.total}`}
              </p>
            )}
          </div>

          <form action="/" method="get" className="competition-filters">
            <label className="filter-field filter-field--search">
              <span>Search competitions</span>
              <input
                type="search"
                name="q"
                placeholder="Title or description"
                defaultValue={q}
              />
            </label>
            <label className="filter-field">
              <span>Category</span>
              <select name="category" defaultValue={category}>
                <option value="">All categories</option>
                {categories.status === "ok" && categories.data.map((item) => (
                  <option key={item.slug} value={item.slug}>
                    {item.name} ({item.competition_count})
                  </option>
                ))}
                {category && categories.status === "ok" &&
                  !categories.data.some((item) => item.slug === category) && (
                    <option value={category}>{category}</option>
                  )}
              </select>
            </label>
            <button className="filter-submit" type="submit">Search</button>
            {hasFilters && <Link className="filter-clear" href="/#competitions">Clear filters</Link>}
          </form>

          {categories.status === "error" && (
            <p className="filter-note" role="status">
              Categories could not be loaded. Search is still available.
            </p>
          )}

          {result.status === "error" ? (
            <div className="state-panel state-panel--error" role="alert">
              <h3>We couldn’t load competitions</h3>
              <p>{result.message}. Please try again in a little while.</p>
            </div>
          ) : competitions.length === 0 ? (
            <div className="state-panel" role="status">
              <h3>
                {page && page.total > 0
                  ? "This page is past the end of the results"
                  : hasFilters
                    ? "No competitions match these filters"
                    : "No competitions to show yet"}
              </h3>
              <p>
                {page && page.total > 0
                  ? "Return to the previous page to continue browsing."
                  : hasFilters
                  ? "Try another search or category to find a competition."
                  : "Reviewed competition titles will appear here when they are available."}
              </p>
              {page && page.total > 0 && offset > 0 && (
                <Link className="filter-clear" href={queryForOffset(previousOffset)}>
                  ← Previous page
                </Link>
              )}
            </div>
          ) : (
            <>
              <div className="competition-list">
                {offset === 0 && <CompetitionCard competition={competitions[0]} apiBaseUrl={process.env.API_BASE_URL ?? ""} featured />}
                {competitions.slice(offset === 0 ? 1 : 0).map((competition) => (
                  <CompetitionCard
                    apiBaseUrl={process.env.API_BASE_URL ?? ""}
                    competition={competition}
                    key={competition.id}
                  />
                ))}
              </div>
              {(offset > 0 || nextOffset < page!.total) && (
                <nav className="competition-pagination" aria-label="Competition pages">
                  {offset > 0 ? (
                    <Link href={queryForOffset(previousOffset)}>← Previous</Link>
                  ) : <span />}
                  <span>Page {Math.floor(offset / page!.limit) + 1} of {Math.ceil(page!.total / page!.limit)}</span>
                  {nextOffset < page!.total ? (
                    <Link href={queryForOffset(nextOffset)}>Next →</Link>
                  ) : <span />}
                </nav>
              )}
            </>
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
