import type { Metadata } from "next";
import { ArchitectureView } from "@/components/views/ArchitectureView";

export const metadata: Metadata = { title: "Architecture | DevLens" };

export default function Page() {
  return <ArchitectureView />;
}
