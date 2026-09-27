// Send a Telegram test message (SPEC §12 Settings). A 409 means Telegram is not configured.
import { useMutation } from "@tanstack/react-query";

import { isApiError, useApi } from "../../api/client";
import { Button, errorMessage } from "../../components/ui";

export const NOT_CONFIGURED = "Telegram is not configured";

export function TelegramTest() {
  const api = useApi();
  const test = useMutation({ mutationFn: () => api.telegramTest() });
  const failure = test.isError ? (isApiError(test.error) && test.error.status === 409 ? NOT_CONFIGURED : errorMessage(test.error)) : null;
  return (
    <div className="stack">
      <div>
        <Button variant="plain" busy={test.isPending} onClick={() => test.mutate()}>
          Send a test message
        </Button>
      </div>
      {test.isSuccess && (
        <p className={`small ${test.data.sent ? "tone-ok" : "tone-bad"}`} role="status">
          {test.data.message}
        </p>
      )}
      {failure && (
        <p className="small tone-bad" role="alert">
          {failure}
        </p>
      )}
    </div>
  );
}
