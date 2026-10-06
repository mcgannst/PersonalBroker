// Renders a piece of the Options page for tests: the usual providers plus an `OptionsApiProvider` with a
// `FakeOptionsApiClient` (or the one given).
import type { ReactElement } from "react";

import { OptionsApiProvider } from "../../api/optionsClient";
import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { renderWithProviders, type RenderWithProvidersOptions } from "../../test/render";

export function renderOptions(ui: ReactElement, options: RenderWithProvidersOptions & { opt?: FakeOptionsApiClient } = {}) {
  const { opt = new FakeOptionsApiClient(), ...rest } = options;
  const result = renderWithProviders(<OptionsApiProvider client={opt}>{ui}</OptionsApiProvider>, rest);
  return { ...result, opt };
}
