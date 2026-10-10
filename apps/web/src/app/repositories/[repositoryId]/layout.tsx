import { Suspense } from "react";
import Link from "next/link";
import { Workstation } from "@/components/workstation/Workstation";
import { isUuid } from "@/lib/format";
import styles from "@/components/ui/ui.module.css";

/**
 * Every workstation route lives under /repositories/[repositoryId]. The id
 * is validated here, before any request, so a malformed URL renders a
 * clear error instead of a backend 422 or a crash.
 */
export default async function RepositoryLayout(
  props: LayoutProps<"/repositories/[repositoryId]">,
) {
  const { repositoryId } = await props.params;

  if (!isUuid(repositoryId)) {
    return (
      <main className={styles.state} style={{ margin: "48px auto" }}>
        <h1 className={styles.stateTitle}>Invalid repository id</h1>
        <p>
          <code>{repositoryId}</code> is not a repository id. Repository ids are
          UUIDs returned by ingestion.
        </p>
        <p>
          <Link href="/workspace" className={styles.button}>
            Open a repository
          </Link>
        </p>
      </main>
    );
  }

  return (
    <Suspense>
      <Workstation repositoryId={repositoryId.toLowerCase()}>
        {props.children}
      </Workstation>
    </Suspense>
  );
}
