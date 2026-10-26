import { connection } from "next/server";

import { APP_NAME } from "@/lib/config";
import { checkHealth } from "@/lib/health";

export const metadata = { title: APP_NAME };

export default async function Home() {
  await connection();
  const health = await checkHealth();
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
    </main>
  );
}
