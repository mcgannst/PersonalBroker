// One pending proposal: its order, risk and reason, a countdown to expiry (server clock), and one-tap
// Approve / Reject (P4-T13; Stephen's decision 3: no confirmation step, the same as Telegram).
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { DecisionOut, ProposalOut } from "../../api/types";
import { Badge, Button, errorMessage, type Tone } from "../../components/ui";
import { fmtDuration, fmtMoney, fmtPrice, secondsUntil } from "../../lib/format";
import { orderLine, proposalHeadline } from "./labels";

export interface PendingProposalProps {
  proposal: ProposalOut;
  /** `Date.parse(server_time) - Date.now()` when the dashboard was read. */
  serverSkewMs: number;
  /** True when a `?proposal=<id>` deep link points at this card. */
  highlighted?: boolean;
  /** Called after the server answered a decision (so the page can keep the message after a refetch). */
  onDecided?: (result: DecisionOut) => void;
}

/** Under this many seconds the countdown turns red. */
const URGENT_SECONDS = 60;

const KIND_TONE: Record<string, Tone> = { entry: "info", stop: "warn", exit: "warn", cancel: "muted" };

export function decisionTone(result: DecisionOut): Tone {
  if (result.blocked || result.proposal.status === "failed") return "bad";
  if (result.already_decided) return "warn";
  return "ok";
}

interface Outcome {
  text: string;
  tone: Tone;
}

export default function PendingProposal({ proposal, serverSkewMs, highlighted = false, onDecided }: PendingProposalProps) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [secondsLeft, setSecondsLeft] = useState(() => secondsUntil(proposal.expires_at, serverSkewMs));
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [decided, setDecided] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const inFlight = useRef(false);
  const cardRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const tick = () => setSecondsLeft(secondsUntil(proposal.expires_at, serverSkewMs));
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [proposal.expires_at, serverSkewMs]);

  useEffect(() => {
    if (highlighted) cardRef.current?.scrollIntoView?.({ block: "center" });
  }, [highlighted]);

  const expired = secondsLeft <= 0;

  async function decide(action: "approve" | "reject") {
    if (inFlight.current || decided || expired) return;
    inFlight.current = true;
    setBusy(action);
    setOutcome(null);
    try {
      const result = action === "approve" ? await api.approve(proposal.id) : await api.reject(proposal.id);
      setDecided(true);
      setOutcome({ text: result.message, tone: decisionTone(result) });
      onDecided?.(result);
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
      void queryClient.invalidateQueries({ queryKey: qk.proposal(proposal.id) });
    } catch (error) {
      setOutcome({ text: errorMessage(error), tone: "bad" });
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  const headline = proposalHeadline(proposal.kind);
  const locked = busy !== null || decided || expired;
  const classes = ["card", "proposal-card", highlighted ? "is-highlighted" : ""].filter(Boolean).join(" ");

  return (
    <article ref={cardRef} className={classes} aria-label={`Proposal ${proposal.id}: ${headline} ${proposal.ticker}`}>
      <div className="row">
        <Badge tone={KIND_TONE[proposal.kind] ?? "info"}>{headline}</Badge>
        <strong className="ticker">{proposal.ticker}</strong>
        <span className="num">{`${proposal.side.toUpperCase()} ${proposal.qty}`}</span>
      </div>
      <div className="row small num">
        <span>{orderLine(proposal)}</span>
        {proposal.stop_loss !== null && <span>{`Stop loss ${fmtPrice(proposal.stop_loss)}`}</span>}
        {proposal.risk_usd !== null && <span>{`Risk ${fmtMoney(proposal.risk_usd)}`}</span>}
      </div>
      <p className="proposal-reason">{proposal.reason}</p>
      <p className="small muted">{`Strategy ${proposal.strategy_key}`}</p>
      {!decided &&
        (expired ? (
          <p className="countdown text-tone tone-bad">Expired, waiting for the server</p>
        ) : (
          <p className={secondsLeft < URGENT_SECONDS ? "countdown num text-tone tone-bad" : "countdown num"}>
            {`Expires in ${fmtDuration(secondsLeft)}`}
          </p>
        ))}
      <div className="row proposal-actions">
        <Button variant="primary" busy={busy === "approve"} disabled={locked} onClick={() => void decide("approve")}>
          Approve
        </Button>
        <Button variant="danger" busy={busy === "reject"} disabled={locked} onClick={() => void decide("reject")}>
          Reject
        </Button>
      </div>
      {outcome && (
        <p role="status" className={`outcome text-tone tone-${outcome.tone}`}>
          {outcome.text}
        </p>
      )}
    </article>
  );
}
