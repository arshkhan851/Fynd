import type { Metadata } from "next";

import { SiteHeader } from "@/components/site-header";
import { SubmissionForm } from "@/components/submission-form";
import { getCompetitionCategories } from "@/lib/competitions";
import { APP_NAME } from "@/lib/config";

export const metadata: Metadata = { title: `Submit a hackathon — ${APP_NAME}` };

export default async function SubmitPage() {
  const categories = await getCompetitionCategories();
  return <><SiteHeader /><main className="page-shell submission-page">
    <header className="submission-hero"><p className="eyebrow">For organizers and students</p><h1 className="editorial-heading">Bring the next <em>big challenge</em> to Fynd.</h1><p>Share a hackathon with the community. Every submission is reviewed before it appears in the competition index.</p></header>
    <SubmissionForm apiBaseUrl={process.env.API_BASE_URL ?? ""} categories={categories.status === "ok" ? categories.data : []} categoryError={categories.status === "error" ? "Categories could not be loaded right now. You can still submit without categories." : undefined} />
  </main></>;
}
