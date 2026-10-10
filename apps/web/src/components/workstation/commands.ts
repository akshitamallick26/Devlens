/**
 * The command bar's grammar. Every command maps to an existing backend
 * capability; anything else is routed to Code Q&A (POST /qa), which is the
 * backend's general question surface. There is no client-side "answer".
 */

export type Command =
  | { kind: "empty" }
  | { kind: "help" }
  | { kind: "impact"; qualifiedName: string }
  | { kind: "ask"; question: string };

export const COMMAND_HELP: { syntax: string; description: string }[] = [
  {
    syntax: "impact <qualified.name>",
    description: "Deterministic impact analysis for a symbol",
  },
  {
    syntax: "ask <question>",
    description: "Code Q&A over this revision (also the default)",
  },
  { syntax: "help", description: "Show these commands" },
];

export function parseCommand(input: string): Command {
  const text = input.trim();
  if (!text) return { kind: "empty" };
  if (text === "help" || text === "?") return { kind: "help" };

  const impact = /^impact\s+(\S.*)$/i.exec(text);
  if (impact) return { kind: "impact", qualifiedName: impact[1].trim() };

  const ask = /^ask\s+(\S.*)$/i.exec(text);
  if (ask) return { kind: "ask", question: ask[1].trim() };

  return { kind: "ask", question: text };
}
