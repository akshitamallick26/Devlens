import type { Metadata } from "next";
import { FilesView } from "@/components/views/FilesView";

export const metadata: Metadata = { title: "Files | DevLens" };

export default function Page() {
  return <FilesView />;
}
