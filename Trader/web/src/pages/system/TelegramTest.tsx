// The Telegram test button (P4-T9 `POST /api/system/telegram-test`): sends one test message through the bot.
import { useMutation } from "@tanstack/react-query";

import { useApi } from "../../api/client";
import { Button, errorMessage } from "../../components/ui";

export function TelegramTest() {
  const api = useApi();
  const send = useMutation({ mutationFn: () => api.telegramTest() });
  return (
    <div className="stack">
      <div>
        <Button busy={send.isPending} onClick={() => send.mutate()}>
          Send a test message
        </Button>
      </div>
      {send.isSuccess && <p className="small">{send.data.message}</p>}
      {send.isError && (
        <p className="small tone-bad" role="alert">
          {errorMessage(send.error)}
        </p>
      )}
    </div>
  );
}
