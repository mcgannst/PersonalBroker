// OPTSIM-T15: the pending prompts: a button per choice, a required text where the prompt needs one, and a
// prompt that was already answered elsewhere (409).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { textPrompt, yesNoPrompt } from "../../test/optionsFixtures";
import { PromptsBanner } from "./PromptsBanner";
import { renderOptions } from "./testRender";

const YES_NO = "Would you buy F today with fresh cash?";
const APPROVE = "Approve SOFI?";

describe("PromptsBanner", () => {
  it("shows each pending prompt with its choices; a choice answers it and the list is read again", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient();
    renderOptions(<PromptsBanner />, { opt });
    const prompt = await screen.findByRole("article", { name: YES_NO });
    expect(screen.getByRole("heading", { name: "Waiting for your answer (2)" })).toBeInTheDocument();
    expect(within(prompt).getByText(/100 shares of F were assigned/)).toBeInTheDocument();
    expect(within(prompt).getAllByRole("button").map((b) => b.textContent)).toEqual(["Yes", "No"]);
    expect(within(prompt).queryByRole("textbox")).toBeNull();
    expect(opt.callsTo("optPrompts")[0]).toEqual([{ status: "pending" }]);

    opt.set("optPrompts", { items: [textPrompt] });
    await user.click(within(prompt).getByRole("button", { name: "No" }));
    await waitFor(() => expect(opt.callsTo("optAnswerPrompt")).toEqual([[12, { choice: "n", text: null }]]));
    await waitFor(() => expect(screen.queryByRole("article", { name: YES_NO })).toBeNull());
    expect(screen.getByRole("article", { name: APPROVE })).toBeInTheDocument();
  });

  it("a prompt that needs text: approving without it is refused here; with it the text is sent", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<PromptsBanner />);
    const prompt = await screen.findByRole("article", { name: APPROVE });
    await user.click(within(prompt).getByRole("button", { name: "Approve" }));
    expect(within(prompt).getByRole("alert")).toHaveTextContent("Write your answer first.");
    expect(opt.callsTo("optAnswerPrompt")).toEqual([]);

    await user.type(within(prompt).getByLabelText("Your answer in words"), "  Profitable, and I would hold it for years. ");
    expect(within(prompt).queryByRole("alert")).toBeNull();
    await user.click(within(prompt).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(opt.callsTo("optAnswerPrompt")).toEqual([[13, { choice: "a", text: "Profitable, and I would hold it for years." }]]));
  });

  it("a choice that needs no text (reject) is sent without one", async () => {
    const user = userEvent.setup();
    const { opt } = renderOptions(<PromptsBanner />);
    const prompt = await screen.findByRole("article", { name: APPROVE });
    await user.click(within(prompt).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(opt.callsTo("optAnswerPrompt")).toEqual([[13, { choice: "r", text: null }]]));
  });

  it("a 409 (already answered, for example on Telegram) is said plainly, with no buttons left", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient().fail("optAnswerPrompt", new ApiError(409, "conflict", "Prompt 12 was already answered."));
    renderOptions(<PromptsBanner />, { opt });
    const prompt = await screen.findByRole("article", { name: YES_NO });
    await user.click(within(prompt).getByRole("button", { name: "Yes" }));
    expect(await within(prompt).findByText("This was already answered.")).toBeInTheDocument();
    expect(within(prompt).queryByRole("button", { name: "Yes" })).toBeNull();
    expect(within(prompt).queryByRole("alert")).toBeNull();
    const reads = opt.callsTo("optPrompts").length;
    await user.click(within(prompt).getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(opt.callsTo("optPrompts").length).toBeGreaterThan(reads));
  });

  it("another failure shows the server's message and keeps the choices", async () => {
    const user = userEvent.setup();
    const opt = new FakeOptionsApiClient().fail("optAnswerPrompt", new ApiError(422, "validation", "That choice is not offered."));
    renderOptions(<PromptsBanner />, { opt });
    const prompt = await screen.findByRole("article", { name: YES_NO });
    await user.click(within(prompt).getByRole("button", { name: "Yes" }));
    expect(await within(prompt).findByRole("alert")).toHaveTextContent("That choice is not offered.");
    expect(within(prompt).getByRole("button", { name: "Yes" })).toBeEnabled();
  });

  it("renders nothing when nothing is pending; a ?prompt= that is gone says so", async () => {
    const opt = new FakeOptionsApiClient({ optPrompts: { items: [] } });
    const first = renderOptions(<PromptsBanner />, { opt });
    await waitFor(() => expect(opt.callsTo("optPrompts")).toHaveLength(1));
    expect(first.container).toBeEmptyDOMElement();
    first.unmount();

    renderOptions(<PromptsBanner focusId={99} />, { opt: new FakeOptionsApiClient({ optPrompts: { items: [yesNoPrompt] } }) });
    expect(await screen.findByText("Prompt 99 is no longer waiting for an answer.")).toBeInTheDocument();
  });
});
