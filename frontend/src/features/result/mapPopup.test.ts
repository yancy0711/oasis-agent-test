import { expect, it } from "vitest";
import { popupContent } from "./mapPopup";

it("shows observed socioeconomic values in compact demand-zone popups", () => {
  const hongKong = popupContent({ GEOID: "101", low_income_household_pct: 18.4 }, undefined, false);
  expect(hongKong.textContent).toContain("Low-income HH: 18.4%");
  expect(hongKong.textContent).not.toContain("GRDI");

  const mainland = popupContent({ GEOID: "102", deprivation_index: 0 }, undefined, false);
  expect(mainland.textContent).toContain("GRDI: 0/100");
  expect(mainland.textContent).not.toContain("Low-income HH");

  const us = popupContent({ GEOID: "103", poverty_pct: 15 }, undefined, false);
  expect(us.textContent).toContain("Poverty: 15%");
});
