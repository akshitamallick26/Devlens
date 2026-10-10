"use client";

import { useEffect } from "react";

/**
 * Marks [data-reveal] blocks visible as they enter the viewport. Content is
 * only hidden once this has mounted (the container gets data-reveal-ready),
 * so without JavaScript, or before hydration, every block is visible.
 */
export function RevealOnScroll({ containerId }: { containerId: string }) {
  useEffect(() => {
    const container = document.getElementById(containerId);
    if (!container) return;
    const blocks = Array.from(
      container.querySelectorAll<HTMLElement>("[data-reveal]"),
    );
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.setAttribute("data-visible", "true");
            observer.unobserve(entry.target);
          }
        }
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.05 },
    );
    blocks.forEach((block) => observer.observe(block));
    container.setAttribute("data-reveal-ready", "true");
    return () => {
      observer.disconnect();
      container.removeAttribute("data-reveal-ready");
    };
  }, [containerId]);
  return null;
}
