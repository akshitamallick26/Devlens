import type { Metadata } from "next";
import { HomeView } from "@/components/views/HomeView";

export const metadata: Metadata = {
  title: "DevLens workspace",
};

export default function Workspace() {
  return <HomeView />;
}
