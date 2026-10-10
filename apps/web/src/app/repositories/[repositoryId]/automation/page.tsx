import type { Metadata } from "next";
import { AutomationView } from "@/components/views/AutomationView";

export const metadata: Metadata = { title: "Change analysis | DevLens" };

export default function Page() {
  return <AutomationView />;
}
