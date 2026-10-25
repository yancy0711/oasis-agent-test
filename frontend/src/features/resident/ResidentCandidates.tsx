import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LoadingText } from "../../components/LoadingText";
import { getStreetView } from "../../api/client";
import { CATEGORIES, type Place, type SearchResult } from "./residentApi";

function StreetPreview({ place }: { place: Place }) {
  const [imageFailed, setImageFailed] = useState(false);
  const query = useQuery({ queryKey: ["resident-streetview", place.longitude, place.latitude],
    queryFn: () => getStreetView(place.latitude, place.longitude), staleTime: 300_000, retry: false });
  const data = query.data;
  if (query.isPending) return <p role="status"><LoadingText>Loading nearby street imagery</LoadingText></p>;
  if (query.isError || imageFailed) return <p>Street imagery could not be loaded. Place details and directions remain available.</p>;
  if (!data?.available || !data.image_url) return <p>{data?.message || "No nearby street imagery available."}</p>;
  return <figure className="resident-street-preview">
    <img src={data.image_url} alt={`Street imagery near ${place.name}`} onError={() => setImageFailed(true)} />
    <figcaption>Nearby street imagery, not a verified facility photo. {data.distance_m != null && `${data.distance_m} m from the mapped point.`}
      {data.creator && ` Photo: ${data.creator}.`} {data.captured_at && ` Captured: ${new Date(data.captured_at).toLocaleDateString("en-US")}.`}
      {data.viewer_url && <a href={data.viewer_url} target="_blank" rel="noreferrer">View on Mapillary</a>}
    </figcaption>
  </figure>;
}

interface Props { emptyMessage: string; result: SearchResult | null; selectedId: string | null; preferred: string[]; fastestId: string | null;
  onSelect: (id: string) => void; onNavigate: (id: string) => void; }
export function ResidentCandidates({ emptyMessage, result, selectedId, preferred, fastestId, onSelect, onNavigate }: Props) {
  const list = useRef<HTMLElement>(null);
  useEffect(() => {
    const card = [...(list.current?.querySelectorAll<HTMLElement>("[data-place-id]") ?? [])]
      .find((element) => element.dataset.placeId === selectedId);
    card?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [selectedId]);
  return <aside ref={list} className="resident-candidate-sidebar" aria-label="Nearby candidate details">
    <h3>{result ? `${result.complete ? "Matching places" : "Partial results"} (${result.candidates.length})` : "Nearby places"}</h3>
    <p>Nearest first by network travel time.</p>
    {!result && <p>{emptyMessage}</p>}
    {result?.candidates.length === 0 && <p>{result.complete ? "No matches within the selected time and radius. Adjust the filters and search again." : "No matches confirmed yet. Some candidates could not be checked; retry the search."}</p>}
    {result?.candidates.map((place, index) => <article key={place.id} data-place-id={place.id}
      className={`resident-place${selectedId === place.id ? " is-selected" : ""}`}>
      <button className="resident-place-select" aria-pressed={selectedId === place.id} onClick={() => onSelect(place.id)}>
        <span className="resident-category-dot" style={{ background: CATEGORIES[place.category].color }} />{index + 1}. {place.name}
      </button>
      {place.id === fastestId && <span className="resident-fastest-badge" title="Fastest among the compared destination routes">Fastest</span>}
      <p>{CATEGORIES[place.category].label} / {place.duration_minutes.toFixed(1)} min {result.mode}</p>
      {preferred.includes(place.category) && <small>Matches your preferred category</small>}
      <small>Opening hours: {place.opening_hours}</small>
      <small>Access: {place.access} / Fee: {place.fee} / Indoor: {place.indoor}</small>
      <div className="button-row"><button aria-label={`Route to ${place.name}`} onClick={() => onNavigate(place.id)}>Route</button>
        <a href={place.osm_url} target="_blank" rel="noreferrer">OSM details</a></div>
      {selectedId === place.id && <StreetPreview key={place.id} place={place} />}
    </article>)}
  </aside>;
}
