"use client";

import { type FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { COMMAND_HELP, parseCommand } from "./commands";
import { useWorkspace } from "./WorkspaceContext";
import styles from "./workstation.module.css";

/**
 * Persistent command surface. It only navigates to the view that owns a
 * real backend capability, passing the input along; the view performs the
 * request and shows its loading/error states.
 */
export function CommandBar() {
  const router = useRouter();
  const { href } = useWorkspace();
  const [value, setValue] = useState("");
  const [showHelp, setShowHelp] = useState(false);

  function submit(event: FormEvent) {
    event.preventDefault();
    const command = parseCommand(value);
    if (command.kind === "empty") return;
    if (command.kind === "help") {
      setShowHelp(true);
      return;
    }
    setShowHelp(false);
    setValue("");
    if (command.kind === "impact") {
      router.push(href("impact", { qualified_name: command.qualifiedName }));
    } else {
      router.push(href("qa", { q: command.question }));
    }
  }

  return (
    <div className={styles.command}>
      <form className={styles.commandForm} onSubmit={submit} role="search">
        <span className={styles.commandPrompt} aria-hidden="true">
          &gt;
        </span>
        <label htmlFor="command-input" className="visually-hidden">
          Command
        </label>
        <input
          id="command-input"
          className={styles.commandInput}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Ask about this revision, or: impact package.module.function   (help for commands)"
          autoComplete="off"
          spellCheck={false}
        />
        <button type="submit" className="visually-hidden">
          Run
        </button>
      </form>
      {showHelp ? (
        <ul className={styles.commandHelp} aria-label="Supported commands">
          {COMMAND_HELP.map((item) => (
            <li key={item.syntax}>
              <code>{item.syntax}</code>: {item.description}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
