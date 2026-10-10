import type { Metadata } from "next";
import { EmptyState } from "@/components/ui/primitives";
import { SessionDetailView } from "@/components/views/SessionsView";
import { isUuid } from "@/lib/format";

export const metadata: Metadata = { title: "Investigation | DevLens" };

export default async function Page(
  props: PageProps<"/repositories/[repositoryId]/sessions/[sessionId]">,
) {
  const { sessionId } = await props.params;
  if (!isUuid(sessionId)) {
    return (
      <EmptyState title="Invalid session id">Session ids are UUIDs.</EmptyState>
    );
  }
  return <SessionDetailView sessionId={sessionId.toLowerCase()} />;
}
