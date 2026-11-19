import { useImperativeHandle, useRef, useState, type Ref } from "react";
import { uploadDemand } from "../../api/client";
import type { DemandUpload } from "../../api/models";
import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import { useAppStore } from "../../store/appStore";

const fieldAliases = {
  id: ["unit_id", "geoid", "stpug", "id", "分區編號", "分区编号"],
  population: ["total_population", "population", "t_pop", "總人口", "总人口"],
  older: ["pop_65plus", "elderly_population", "age_5", "65歲及以上人口", "65岁及以上人口"],
};

export type DemandUploadHandle = { openFile: (file: File) => void };

export function RegionalDataUpload({ place, active, onConfirm, onRemove, ref }: {
  place?: string;
  active?: DemandUpload | null;
  onConfirm: (data: DemandUpload) => void;
  onRemove?: () => void;
  ref?: Ref<DemandUploadHandle>;
}) {
  const { t } = useWorkspacePreferences();
  const [file, setFile] = useState<File>();
  const [columns, setColumns] = useState<string[]>([]);
  const [placeOverride, setPlaceOverride] = useState<string>();
  const [epsg, setEpsg] = useState("4326");
  const [coordinateSystem, setCoordinateSystem] = useState<"epsg" | "gcj02">("epsg");
  const [year, setYear] = useState("");
  const [fields, setFields] = useState({ id: "", population: "", older: "" });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const readToken = useRef(0);
  const disclosure = useRef<HTMLDetailsElement>(null);
  const name = placeOverride ?? place ?? "";

  async function chooseFile(next?: File) {
    if (pending) return;
    const token = ++readToken.current;
    setFile(undefined);
    setColumns([]);
    setError("");
    if (!next) return;
    try {
      if (next.size > 20 * 1024 * 1024) throw new Error(t("Demand GeoJSON must be 20 MiB or smaller."));
      const source = JSON.parse(await next.text()) as { type?: string; features?: { properties?: object }[]; crs?: { properties?: { name?: string } } };
      if (token !== readToken.current) return;
      if (source.type !== "FeatureCollection" || !Array.isArray(source.features) || !source.features.length) {
        throw new Error(t("Choose a GeoJSON FeatureCollection of demand polygons."));
      }
      const names = Array.from(new Set(source.features.flatMap((feature) => Object.keys(feature?.properties ?? {}))));
      const guess = (aliases: string[]) => names.find((column) => aliases.includes(column.toLowerCase())) ?? "";
      setFields({ id: guess(fieldAliases.id), population: guess(fieldAliases.population), older: guess(fieldAliases.older) });
      const declaredCrs = source.crs?.properties?.name ?? "";
      setCoordinateSystem(/gcj[- ]?02/i.test(declaredCrs) ? "gcj02" : "epsg");
      setEpsg(declaredCrs.match(/EPSG[\s:/]*(\d+)$/i)?.[1] ?? "4326");
      setColumns(names);
      setPlaceOverride(undefined);
      setFile(next);
    } catch (reason) {
      if (token === readToken.current) setError(reason instanceof Error ? reason.message : "Could not read GeoJSON.");
    }
  }

  useImperativeHandle(ref, () => ({
    openFile(next) {
      if (disclosure.current) disclosure.current.open = true;
      void chooseFile(next);
    },
  }));

  async function submit() {
    if (!file || !fields.id || !fields.population || !name.trim() || pending) return;
    const form = new FormData();
    form.set("file", file);
    form.set("place_name", name.trim());
    form.set("id_field", fields.id);
    form.set("population_field", fields.population);
    form.set("older_field", fields.older);
    form.set("coordinate_system", coordinateSystem);
    if (coordinateSystem === "epsg") form.set("epsg", epsg);
    if (year) form.set("year", year);
    setError("");
    setPending(true);
    const taskVersion = useAppStore.getState().taskVersion;
    try {
      const data = await uploadDemand(form);
      if (useAppStore.getState().taskVersion === taskVersion) onConfirm(data);
      setFile(undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not upload demand data.");
    } finally {
      setPending(false);
    }
  }

  return <details className="workbench-disclosure" ref={disclosure}>
    <summary>{t("Demand polygons and population")}</summary>
    <p>{t("Upload GeoJSON with one polygon per demand zone and a population count in its properties.")}</p>
    <label className="field"><span>{t("Demand GeoJSON")}</span><input type="file" accept=".geojson,application/geo+json" disabled={pending}
      onChange={(event) => { void chooseFile(event.currentTarget.files?.[0]); event.currentTarget.value = ""; }} /></label>
    {file && <fieldset className="regional-field-mapping" disabled={pending}>
      <legend>{t("Confirm field mapping")}</legend>
      <label className="field"><span>{t("Study area")}</span><input value={name} onChange={(event) => setPlaceOverride(event.target.value)} maxLength={250} /></label>
      <label className="field"><span>{t("Demand coordinates")}</span>
        <select value={coordinateSystem} onChange={(event) => setCoordinateSystem(event.target.value === "gcj02" ? "gcj02" : "epsg")}>
          <option value="epsg">{t("EPSG coordinate system")}</option>
          <option value="gcj02">{t("GCJ-02 (mainland China, approximate conversion)")}</option>
        </select>
      </label>
      {coordinateSystem === "epsg" && <label className="field"><span>{t("Input EPSG code")}</span><input type="number" min={1} value={epsg} onChange={(event) => setEpsg(event.target.value)} /></label>}
      <label className="field"><span>{t("Data year (optional)")}</span><input type="number" min={1900} max={2100} value={year} onChange={(event) => setYear(event.target.value)} /></label>
      {([["id", "Zone ID"], ["population", "Total population"], ["older", "Age 65+ population (optional)"]] as const).map(([key, label]) =>
        <label className="field" key={key}><span>{t(label)}</span><select aria-label={t(label)} value={fields[key]} onChange={(event) => setFields({ ...fields, [key]: event.target.value })}>
          <option value="">{t(key === "older" ? "Unavailable" : "Choose a field")}</option>
          {columns.map((column) => <option value={column} key={column}>{column}</option>)}
        </select></label>
      )}
      <button type="button" disabled={pending || !fields.id || !fields.population || !name.trim() || (coordinateSystem === "epsg" && !epsg)} onClick={() => void submit()}>
        {t(pending ? "Validating upload" : "Confirm and use uploaded demand")}
      </button>
    </fieldset>}
    {active && <div className="upload-status workbench-confirmed">
      <span role="status">{active.filename} · {active.units} {t("demand zones")}{active.year ? ` · ${active.year}` : ""}</span>
      {onRemove && <button type="button" className="upload-remove" title={t("Remove uploaded demand polygons")}
        aria-label={t("Remove uploaded demand polygons")} disabled={pending} onClick={onRemove}>×</button>}
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </details>;
}
