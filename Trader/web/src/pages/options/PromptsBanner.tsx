// The questions waiting for Stephen (OPTSIM-T15), shown above every tab of the Options page: each pending
// prompt with a button per choice, and a text box when the prompt needs a written answer. `/options?prompt=<id>`
// (the Telegram link for written answers) marks that prompt and scrolls to it.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { isApiError } from "../../api/client";
import { OPT_SLOW_MS, oqk, useOptionsApi, type OptPromptsQuery } from "../../api/optionsClient";
import type { OptPromptAnswerIn, OptPromptOut } from "../../api/types";
import { Button, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { useRefreshOptions } from "./shared";

const PENDING: OptPromptsQuery = { status: "pending" };

/** With `needs_text`, these choices (approve / accept, and "write on the web") need a non-empty text. */
export const TEXT_CHOICES: ReadonlySet<string> = new Set(["a", "w"]);

function PromptCard({ prompt, focused }: { prompt: OptPromptOut; focused: boolean }) {
  const api = useOptionsApi();
  const refresh = useRefreshOptions();
  const ref = useRef<HTMLElement>(null);
  const [text, setText] = useState("");
  const [missing, setMissing] = useState(false);
  const answer = useMutation({ mutationFn: (body: OptPromptAnswerIn) => api.optAnswerPrompt(prompt.id, body), onSuccess: refresh });
  const already = answer.isError && isApiError(answer.error) && answer.error.status === 409;

  useEffect(() => {
    if (focused) ref.current?.scrollIntoView?.({ block: "start" });
  }, [focused]);

  const choose = (code: string) => {
    const written = text.trim();
    if (prompt.needs_text && TEXT_CHOICES.has(code) && written === "") {
      setMissing(true);
      return;
    }
    setMissing(false);
    answer.mutate({ choice: code, text: written === "" ? null : written });
  };

  const textId = `opt-prompt-${prompt.id}-text`;
  return (
    <article ref={ref} className={focused ? "opt-prompt is-focused stack" : "opt-prompt stack"} aria-label={prompt.title}>
      <header>
        <strong>{prompt.title}</strong>
        <div className="small muted">
          {prompt.source} · asked {fmtDateTime(prompt.asked_at)}
        </div>
      </header>
      {prompt.body && <p className="opt-prompt-body">{prompt.body}</p>}
      {already ? (
        <div className="row">
          <p className="small opt-notice tone-warn" role="status">
            This was already answered.
          </p>
          <Button onClick={refresh}>Refresh</Button>
        </div>
      ) : (
        <>
          {prompt.needs_text && (
            <label htmlFor={textId}>
              Your answer in words
              <textarea
                id={textId}
                rows={3}
                maxLength={2000}
                value={text}
                disabled={answer.isPending}
                aria-invalid={missing || undefined}
                onChange={(e) => {
                  setText(e.target.value);
                  setMissing(false);
                }}
              />
            </label>
          )}
          <div className="row">
            {prompt.choices.map((c) => (
              <Button key={c.code} busy={answer.isPending} onClick={() => choose(c.code)}>
                {c.label}
              </Button>
            ))}
          </div>
          {missing && (
            <p className="small tone-bad opt-notice" role="alert">
              Write your answer first.
            </p>
          )}
          {answer.isError && (
            <p className="small tone-bad opt-notice" role="alert">
              {errorMessage(answer.error)}
            </p>
          )}
        </>
      )}
    </article>
  );
}

export function PromptsBanner({ focusId = null }: { focusId?: number | null }) {
  const api = useOptionsApi();
  const q = useQuery({ queryKey: oqk.prompts(PENDING), queryFn: () => api.optPrompts(PENDING), refetchInterval: OPT_SLOW_MS });
  // An error here (no options run, the server away) is already shown by the tab below.
  if (!q.data) return null;
  const items = q.data.items.filter((p) => p.status === "pending");
  const gone = focusId !== null && !items.some((p) => p.id === focusId);
  if (items.length === 0 && !gone) return null;
  return (
    <section className="opt-prompts stack" aria-label="Waiting for your answer">
      {items.length > 0 && <h2 className="opt-subhead">Waiting for your answer ({items.length})</h2>}
      {gone && (
        <p className="small muted" role="status">
          Prompt {focusId} is no longer waiting for an answer.
        </p>
      )}
      {items.map((p) => (
        <PromptCard key={p.id} prompt={p} focused={p.id === focusId} />
      ))}
    </section>
  );
}
