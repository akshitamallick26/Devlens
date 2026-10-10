import type { Metadata } from "next";
import { OverviewView } from "@/components/views/OverviewView";

export const metadata: Metadata = { title: "Overview | DevLens" };

export default function Page() {
  return <OverviewView />;
}
