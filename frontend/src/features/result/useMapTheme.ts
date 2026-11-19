import { useEffect, type RefObject } from "react";
import type { Map } from "maplibre-gl";
import { useWorkspacePreferences } from "../../app/WorkspacePreferences";

export function useMapTheme(mapRef: RefObject<Map | null>, ready: boolean) {
  const { theme } = useWorkspacePreferences();
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const dark = theme === "dark";
    if (map.getLayer("background")) map.setPaintProperty("background", "background-color", dark ? "#171e27" : "#f3f2ef");
    if (map.getLayer("carto")) {
      map.setPaintProperty("carto", "raster-saturation", dark ? -0.8 : 0);
      map.setPaintProperty("carto", "raster-brightness-min", dark ? 0.12 : 0);
      map.setPaintProperty("carto", "raster-brightness-max", dark ? 0.35 : 1);
    }
  }, [mapRef, ready, theme]);
}
