import { connection } from "next/server";

import { APP_NAME } from "@/lib/config";
import { getCompetitions } from "@/lib/competitions";
import { checkHealth } from "@/lib/health";

export const metadata = { title: APP_NAME };

export default async function Home() {
  await connection();
  const [health, competitions] = await Promise.all([
    checkHealth(),
    getCompetitions(),
  ]);
  const ok = health.status === "ok";

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 p-8">
      <h1 className="text-4xl font-semibold tracking-tight">{APP_NAME}</h1>
      <p className="text-center text-neutral-500">
        Hackathon and competition discovery.
      </p>
      <div
        role="status"
        className={`rounded-lg border px-4 py-3 text-sm ${
          ok
            ? "border-green-600 text-green-700 dark:text-green-400"
            : "border-red-600 text-red-700 dark:text-red-400"
        }`}
      >
        {health.status === "ok"
          ? "API status: connected"
          : `API status: unreachable (${health.message})`}
      </div>
      <section className="w-full max-w-3xl space-y-4" aria-labelledby="competitions-heading">
        <div className="flex items-baseline justify-between gap-4">
          <h2 id="competitions-heading" className="text-2xl font-semibold">
            Competitions
          </h2>
          {competitions.status === "ok" && (
            <p className="text-sm text-neutral-500">
              {competitions.data.total} total
            </p>
          )}
        </div>
        {competitions.status === "error" ? (
          <p role="alert" className="rounded-lg border border-red-600 p-4 text-red-700 dark:text-red-400">
            Could not load competitions: {competitions.message}
          </p>
        ) : competitions.data.items.length === 0 ? (
          <p role="status" className="rounded-lg border p-4 text-neutral-500">
            No competitions with reviewed titles are available yet.
          </p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {competitions.data.items.map((competition) => {
              const starts =
                competition.event_starts_on ?? competition.event_starts_at;
              const deadline =
                competition.registration_deadline_on ??
                competition.registration_deadline_at;
              return (
                <li key={competition.id} className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <h3 className="font-medium">{competition.title}</h3>
                    <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-neutral-500">
                      {competition.format && <span>{competition.format}</span>}
                      {starts && <span>Starts {starts}</span>}
                      {deadline && <span>Registration closes {deadline}</span>}
                    </div>
                  </div>
                  {competition.official_url && (
                    <a
                      className="text-sm underline underline-offset-4"
                      href={competition.official_url}
                      rel="noreferrer"
                      target="_blank"
                    >
                      Official site
                    </a>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </main>
  );
}
