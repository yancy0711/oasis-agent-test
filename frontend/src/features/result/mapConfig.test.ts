import { describe, expect, it } from "vitest";
import type { StyleSpecification } from "maplibre-gl";
import { createBaseMapStyle } from "./mapConfig";

describe("CARTO basemap configuration", () => {
  it("encodes the configured key in tile requests and retains attribution", () => {
    const style = createBaseMapStyle("  test&key=value  ") as StyleSpecification;
    expect(style.sources.carto).toMatchObject({
      tiles: ["https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png?key=test%26key%3Dvalue"],
      attribution: "© OpenStreetMap contributors · © CARTO",
    });
  });

  it("keeps a neutral canvas without making unauthenticated tile requests", () => {
    const style = createBaseMapStyle("  ") as StyleSpecification;
    expect(style.sources).toEqual({});
    expect(style.layers.map((layer) => layer.type)).toEqual(["background"]);
  });
});
