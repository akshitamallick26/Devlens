import type { Metadata } from "next";
import { DocumentationView } from "@/components/views/DocumentationView";

export const metadata: Metadata = { title: "Documentation | DevLens" };

export default function Page() {
  return <DocumentationView />;
}
