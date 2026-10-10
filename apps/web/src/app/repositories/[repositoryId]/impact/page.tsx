import type { Metadata } from "next";
import { ImpactView } from "@/components/views/ImpactView";

export const metadata: Metadata = { title: "Impact analysis | DevLens" };

export default function Page() {
  return <ImpactView />;
}
