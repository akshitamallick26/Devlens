import { apiBaseUrl } from "@/lib/env";
import styles from "./page.module.css";

export default function Home() {
  return (
    <div className={styles.page}>
      <main className={styles.main}>
        <h1>DevLens</h1>
        <p>Frontend foundation running.</p>
        <p>
          <code>API base URL:</code> {apiBaseUrl}
        </p>
      </main>
    </div>
  );
}
