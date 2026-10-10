import type { Metadata } from "next";
import { TestingView } from "@/components/views/TestingView";

export const metadata: Metadata = { title: "Testing | DevLens" };

export default function Page() {
  return <TestingView />;
}
