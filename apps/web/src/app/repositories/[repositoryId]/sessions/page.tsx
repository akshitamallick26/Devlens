import type { Metadata } from "next";
import { SessionsView } from "@/components/views/SessionsView";

export const metadata: Metadata = { title: "Investigations | DevLens" };

export default function Page() {
  return <SessionsView />;
}
