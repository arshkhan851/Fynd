import { SiteHeader } from "@/components/site-header";
import { SavedOpportunitiesList } from "@/components/saved-opportunities-list";

export default function SavedOpportunitiesPage() {
  return (
    <>
      <SiteHeader />
      <SavedOpportunitiesList apiBaseUrl={process.env.API_BASE_URL ?? ""} />
    </>
  );
}
