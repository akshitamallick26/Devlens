import type { Metadata } from "next";
import { LandingPage } from "@/components/landing/LandingPage";

const title = "DevLens | Understand your code before you change it";
const description =
  "AI-powered codebase intelligence for understanding structure, dependencies, and change impact.";

export const metadata: Metadata = {
  title,
  description,
  openGraph: { title, description, type: "website" },
  twitter: { card: "summary", title, description },
};

export default function Home() {
  return <LandingPage />;
}
