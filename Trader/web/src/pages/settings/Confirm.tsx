// An inline "are you sure?" step (P4-T15). Stephen's decision (2026-09-27): only switching to Auto mode,
// resetting a kill switch and pausing ask for confirmation. Inline rather than window.confirm, so it works
// the same on a phone and in tests.
import type { ReactNode } from "react";

import { Button } from "../../components/ui";

export function Confirm({
  message,
  confirmLabel,
  onConfirm,
  onCancel,
  busy = false,
  variant = "danger",
}: {
  message: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
  variant?: "primary" | "danger";
}) {
  return (
    <div className="confirm stack" role="alertdialog" aria-label={confirmLabel}>
      <p>{message}</p>
      <div className="row">
        <Button variant={variant} busy={busy} onClick={onConfirm}>
          {confirmLabel}
        </Button>
        <Button variant="plain" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
