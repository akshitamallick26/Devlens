import type { Metadata } from "next";
import { QAView } from "@/components/views/QAView";

export const metadata: Metadata = { title: "Code Q&A | DevLens" };

export default function Page() {
  return <QAView />;
}
