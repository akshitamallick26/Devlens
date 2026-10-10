import { redirect } from "next/navigation";

export default async function RepositoryIndex(
  props: PageProps<"/repositories/[repositoryId]">,
) {
  const { repositoryId } = await props.params;
  const { revision } = await props.searchParams;
  const query =
    typeof revision === "string"
      ? `?revision=${encodeURIComponent(revision)}`
      : "";
  redirect(
    `/repositories/${encodeURIComponent(repositoryId)}/overview${query}`,
  );
}
