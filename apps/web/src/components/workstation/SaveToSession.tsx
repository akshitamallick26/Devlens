"use client";

import { type FormEvent, useState } from "react";
import {
  addContextRef,
  createSession,
  listSessions,
} from "@/lib/api/endpoints";
import type { ContextRefRequest } from "@/lib/api/types";
import { useAction, useResource } from "@/lib/useApi";
import { ErrorState, ui } from "@/components/ui/primitives";
import { type Selection, useWorkspace } from "./WorkspaceContext";
import styles from "./workstation.module.css";

/**
 * Maps a selection to the backend context-reference type that can hold it,
 * or null when the backend has no reference type for it (evidence items,
 * artifacts). Graph selections keep their focus in graph_camera_state, the
 * only field graph_focus references carry.
 */
export function contextRefFor(
  selection: Selection,
  revisionId: string,
): ContextRefRequest | null {
  if (selection.symbolId && selection.graphFocus) {
    return {
      ref_type: "graph_focus",
      graph_camera_state: {
        focus_symbol_id: selection.symbolId,
        direction: selection.graphFocus.direction,
        depth: selection.graphFocus.depth,
        revision_id: revisionId,
      },
    };
  }
  if (selection.findingId)
    return { ref_type: "finding_reference", finding_id: selection.findingId };
  if (selection.analysisRunId) {
    return {
      ref_type: "analysis_reference",
      analysis_run_id: selection.analysisRunId,
    };
  }
  if (selection.symbolId)
    return { ref_type: "selected_symbol", symbol_id: selection.symbolId };
  if (selection.fileId)
    return { ref_type: "selected_file", file_id: selection.fileId };
  return null;
}

/**
 * Explicit "save to investigation" action. Nothing is written until the
 * user submits; opening the form only reads the session list.
 */
export function SaveToSession({ selection }: { selection: Selection }) {
  const { repositoryId, overview } = useWorkspace();
  const revisionId =
    overview.status === "success" ? overview.data.revision.revision_id : null;
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState("new");
  const [title, setTitle] = useState("");
  const sessions = useResource(
    open ? `${repositoryId}:sessions` : null,
    (signal) => listSessions(repositoryId, { limit: 10 }, signal),
  );
  const save = useAction(
    async (
      signal,
      ref: ContextRefRequest,
      sessionId: string,
      newTitle: string,
    ) => {
      let id = sessionId;
      if (id === "new") {
        if (!revisionId) throw new Error("no revision");
        id = (
          await createSession(
            repositoryId,
            { revision_id: revisionId, title: newTitle },
            signal,
          )
        ).id;
      }
      await addContextRef(repositoryId, id, ref, signal);
      return id;
    },
  );

  if (!revisionId) return null;
  const ref = contextRefFor(selection, revisionId);
  if (!ref) return null;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !ref ||
      save.status === "success" ||
      (target === "new" && !title.trim())
    )
      return;
    save.run(ref, target, title.trim());
  }

  if (!open) {
    return (
      <button type="button" className={ui.button} onClick={() => setOpen(true)}>
        Save to investigation
      </button>
    );
  }

  return (
    <form
      className={styles.saveForm}
      onSubmit={submit}
      aria-label="Save to investigation"
    >
      <label className={ui.label}>
        Investigation
        <select
          className={ui.input}
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        >
          <option value="new">New investigation</option>
          {sessions.status === "success"
            ? sessions.data.sessions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title ?? "Untitled session"}
                </option>
              ))
            : null}
        </select>
      </label>
      {target === "new" ? (
        <label className={ui.label}>
          Title
          <input
            className={ui.input}
            value={title}
            maxLength={200}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="What are you investigating?"
          />
        </label>
      ) : null}
      <p className={ui.faint}>
        Saves this as a {ref.ref_type.replace(/_/g, " ")} reference.
      </p>
      <div className={styles.panelActions}>
        <button
          type="submit"
          className={`${ui.button} ${ui.buttonPrimary}`}
          disabled={
            // Saved once: a second submit would add a duplicate reference
            // (graph_focus references are not deduplicated by the backend).
            save.status === "loading" ||
            save.status === "success" ||
            (target === "new" && !title.trim())
          }
        >
          Save
        </button>
        <button
          type="button"
          className={`${ui.button} ${ui.buttonGhost}`}
          onClick={() => setOpen(false)}
        >
          Cancel
        </button>
      </div>
      {save.status === "success" ? (
        <p role="status">Saved to the investigation.</p>
      ) : null}
      {save.status === "error" ? <ErrorState error={save.error} /> : null}
    </form>
  );
}
