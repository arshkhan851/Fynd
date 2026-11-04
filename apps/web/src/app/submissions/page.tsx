import { SiteHeader } from "@/components/site-header";
import { SubmissionsList } from "@/components/submissions-list";

export default async function MySubmissionsPage({ searchParams }: { searchParams: Promise<{ created?: string }> }) {
  const params = await searchParams;
  return <><SiteHeader /><SubmissionsList apiBaseUrl={process.env.API_BASE_URL ?? ""} created={params.created} /></>;
}
