import { SiteHeader } from "@/components/site-header";
import { AdminSubmissionReview } from "@/components/admin-submission-review";

export default async function AdminSubmissionPage({ params }: { params: Promise<{ submissionId: string }> }) {
  const { submissionId } = await params;
  const parsedId = /^\d+$/.test(submissionId) ? Number(submissionId) : 0;
  return <><SiteHeader /><AdminSubmissionReview apiBaseUrl={process.env.API_BASE_URL ?? ""} submissionId={parsedId} /></>;
}
