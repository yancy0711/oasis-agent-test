import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { getConfig } from "../../api/client";
import { configKeys } from "../../api/queryKeys";
import { createBaseMapStyle } from "./mapConfig";

export function useBasemapStyle() {
  const { data } = useQuery({ queryKey: configKeys.detail, queryFn: getConfig, staleTime: Infinity });
  const key = data?.carto_basemap_api_key ?? "";
  return useMemo(() => createBaseMapStyle(key), [key]);
}
