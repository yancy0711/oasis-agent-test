/// <reference lib="dom" />
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MapView } from "../src/features/result/MapView";
import "../src/styles/globals.css";
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient()}><MapView initialRouteMode="driving" url="/test-map.geojson" runId="route-test" /></QueryClientProvider>,
);
