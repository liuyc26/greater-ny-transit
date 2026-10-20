// Greater NY Live Transit: polls /api/vehicles and animates vehicles along their tracks.

import { FreeMotion, Track, TrackMotion } from "./track.js";

// Display settings per agency. Poll intervals come from /api/agencies (they match the API's cache).
const AGENCIES = {
  lirr: { label: "LIRR", color: "#4d8fe6", radius: 4.5, kind: "Train" },
  mnr: { label: "Metro-North", color: "#e8414b", radius: 4.5, kind: "Train" },
  njt_rail: { label: "NJ Transit Rail", color: "#f7941d", radius: 4.5, kind: "Train" },
  njt_bus: { label: "NJ Transit Bus", color: "#b98cf5", radius: 3, kind: "Bus" },
};
const LABEL_MIN_ZOOM = 11; // show train/route numbers next to dots from this zoom in

// Basemap and map-layer colors per theme (panel colors live in style.css).
const THEMES = {
  dark: {
    basemap: "https://tiles.openfreemap.org/styles/dark",
    routeAlpha: 90, busRouteAlpha: 35,
    station: [160, 170, 185, 140], hubStation: [255, 255, 255, 220],
    label: [235, 240, 248, 235], labelOutline: [14, 17, 22, 230],
  },
  light: {
    basemap: "https://tiles.openfreemap.org/styles/positron",
    routeAlpha: 150, busRouteAlpha: 70,
    station: [110, 118, 130, 170], hubStation: [26, 31, 41, 230],
    label: [26, 31, 41, 240], labelOutline: [255, 255, 255, 235],
  },
};
const systemTheme = () => (window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark");
function savedTheme() {
  try {
    const t = localStorage.getItem("theme");
    return t === "light" || t === "dark" ? t : null;
  } catch {
    return null; // storage blocked (private mode etc.)
  }
}

const state = {
  routes: null,
  stopsGeo: null,
  shapes: { geoms: [], ids: {} }, // distinct geometries + "agency:shape_id" -> index
  tracks: new Map(),      // geometry index -> Track (built lazily)
  stops: new Map(),       // "agency:stop_id" -> {name, coord}
  routeInfo: new Map(),   // "agency:route_id" -> {name, color}
  vehicles: new Map(),    // id -> {data, motion, pos}
  feeds: {},              // agency -> FeedStatus
  lastUpdate: {},         // agency -> ms timestamp
  hidden: new Set(),      // agencies toggled off
  dir: "all",
  hoverId: null,          // vehicle under the pointer: its route is highlighted
  theme: savedTheme() ?? systemTheme(),
  clockOffsetS: 0,        // server time - browser time
};
window.__transit = state; // for debugging from the console / browser tests

document.documentElement.dataset.theme = state.theme;

const map = new maplibregl.Map({
  container: "map",
  // OpenFreeMap: free, no API key, no usage limits (CARTO's free tier requires a key).
  style: THEMES[state.theme].basemap,
  center: [-73.9, 40.82],
  zoom: 8.6,
  renderWorldCopies: false,
  attributionControl: { compact: true },
});

/**
 * Limit the map to the service area: zooming out stops when every route line fits on screen,
 * and panning stops at what's visible at that zoom. Depends on the screen size, so it's
 * recomputed on resize. (maxBounds alone can't do this: the viewport must fit inside it, so
 * on a wide screen you'd never see the whole area at once.)
 */
function applyRegionLimits() {
  if (!state.routes) return;
  let w = 180, s = 90, e = -180, n = -90;
  for (const f of state.routes.features) {
    for (const line of f.geometry.coordinates) {
      for (const [x, y] of line) {
        if (x < w) w = x; if (x > e) e = x;
        if (y < s) s = y; if (y > n) n = y;
      }
    }
  }
  const prev = { center: map.getCenter(), zoom: map.getZoom() };
  map.setMaxBounds(null);
  map.setMinZoom(0);
  const fit = map.cameraForBounds([[w, s], [e, n]], { padding: 24 });
  if (!fit) return;
  map.jumpTo(fit);
  const visible = map.getBounds();
  map.setMinZoom(fit.zoom);
  map.setMaxBounds(visible);
  map.jumpTo({ center: prev.center, zoom: Math.max(prev.zoom, fit.zoom) });
}
map.on("resize", applyRegionLimits);
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

const overlay = new deck.MapboxOverlay({
  layers: [],
  getTooltip,
  onHover: ({ object, layer }) => {
    state.hoverId = layer?.id === "vehicles" && object ? object.data.id : null;
  },
});
map.addControl(overlay);
window.__map = map; // for debugging from the console / browser tests

const nowS = () => Date.now() / 1000 + state.clockOffsetS;

// ---------- data ----------

const STATIC_CHECK_MS = 5 * 60_000;

/**
 * The map data is rebuilt daily; shape ids in API responses refer to the current build. Check
 * meta.json periodically and reload when it changes, otherwise an open page draws and highlights
 * the wrong shapes.
 */
async function syncStatic() {
  let version;
  try {
    version = (await fetch("data/meta.json", { cache: "no-store" }).then((r) => r.json())).built_at;
  } catch {
    return;
  }
  if (version === state.staticVersion) return;
  const first = state.staticVersion === undefined;
  await loadStatic(version);
  state.staticVersion = version;
  if (!first) {
    // Motions hold Track objects built from the old shapes: start every vehicle fresh.
    state.tracks.clear();
    state.vehicles.clear();
    applyRegionLimits();
    feedsConfig.forEach(({ id }) => poll(id));
  }
}

async function loadStatic(version) {
  const v = version ? `?v=${version}` : "";
  const [routes, stops, shapes] = await Promise.all(
    ["data/routes.geojson", "data/stops.geojson", "data/shapes.json"].map((u) => fetch(u + v).then((r) => r.json())),
  );
  state.routeInfo.clear();
  state.stops.clear();
  state.routes = routes;
  state.stopsGeo = stops;
  state.shapes = shapes;
  for (const f of routes.features) {
    const p = f.properties;
    const color = hexToRgb(p.color ?? AGENCIES[p.agency]?.color ?? "#cccccc");
    p.rgb = color;
    state.routeInfo.set(`${p.agency}:${p.route_id}`, { name: p.name, color });
  }
  for (const f of stops.features) {
    const p = f.properties;
    state.stops.set(`${p.agency}:${p.stop_id}`, { name: p.name, coord: f.geometry.coordinates });
  }
}

async function poll(agency) {
  try {
    const res = await fetch(`api/vehicles/${agency}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    ingest(agency, await res.json());
  } catch (err) {
    state.feeds[agency] = { ok: false, error: err.message };
    renderPanel();
  }
}

function trackFor(v) {
  if (!v.shape_id) return null;
  const gi = state.shapes.ids[`${v.agency}:${v.shape_id}`];
  if (gi === undefined) return null;
  if (!state.tracks.has(gi)) {
    const coords = state.shapes.geoms[gi];
    state.tracks.set(gi, coords.length > 1 ? new Track(coords) : null);
  }
  return state.tracks.get(gi);
}

function ingest(agency, data) {
  state.clockOffsetS = data.generated_at - Date.now() / 1000;
  const t = nowS();
  const seen = new Set();
  for (const v of data.vehicles) {
    seen.add(v.id);
    let rec = state.vehicles.get(v.id);
    const track = trackFor(v);
    // New vehicle, or it switched trips/shapes: start a fresh motion.
    if (!rec || rec.data.shape_id !== v.shape_id || rec.data.trip_id !== v.trip_id) {
      rec = { motion: track ? new TrackMotion(track) : new FreeMotion(v.lon, v.lat) };
      state.vehicles.set(v.id, rec);
    }
    rec.data = v;
    const nextStop = state.stops.get(`${v.agency}:${v.next_stop_id}`);
    if (!rec.motion.observe(v, nextStop?.coord, t)) {
      // Report isn't on the expected track: follow raw positions instead.
      rec.motion = new FreeMotion(v.lon, v.lat);
      rec.motion.observe(v);
    }
  }
  // A failed feed returns no vehicles: keep showing the last known ones rather than blanking.
  const status = data.feeds[agency];
  if (status?.ok) {
    for (const [id, rec] of state.vehicles) if (rec.data.agency === agency && !seen.has(id)) state.vehicles.delete(id);
    state.lastUpdate[agency] = Date.now();
  }
  state.feeds[agency] = status;
  renderPanel();
}

// ---------- layers ----------

function frame() {
  render();
  requestAnimationFrame(frame);
}

function render() {
  const t = nowS();
  const visible = [];
  for (const rec of state.vehicles.values()) {
    rec.pos = rec.motion.step(t); // keep advancing even when filtered out
    if (state.dir !== "all" && rec.data.direction !== state.dir) continue;
    if (state.hidden.has(rec.data.agency)) continue;
    visible.push(rec);
  }

  const hovered = state.hoverId && state.vehicles.get(state.hoverId);
  const highlight = hovered ? highlightPaths(hovered) : [];
  const dim = highlight.length > 0;
  const theme = THEMES[state.theme];

  overlay.setProps({
    layers: [
      new deck.GeoJsonLayer({
        id: "routes",
        data: visibleFeatures("routes"),
        stroked: false,
        // Buses: fainter, so the rail network stays readable under ~100 bus routes.
        // Everything fades further while a vehicle's route is highlighted.
        getLineColor: (f) => [
          ...f.properties.rgb,
          (f.properties.agency === "njt_bus" ? theme.busRouteAlpha : theme.routeAlpha) / (dim ? 3 : 1),
        ],
        lineWidthUnits: "pixels",
        getLineWidth: (f) => (f.properties.agency === "njt_bus" ? 1 : 1.5),
        updateTriggers: { getLineColor: [dim, state.theme] },
      }),
      new deck.PathLayer({
        id: "highlight-glow",
        data: highlight,
        getPath: (p) => p.path,
        getColor: (p) => [...p.color, 70],
        widthUnits: "pixels",
        getWidth: 9,
        capRounded: true,
        jointRounded: true,
      }),
      new deck.PathLayer({
        id: "highlight",
        data: highlight,
        getPath: (p) => p.path,
        getColor: (p) => [...p.color, 255],
        widthUnits: "pixels",
        getWidth: 3,
        capRounded: true,
        jointRounded: true,
      }),
      new deck.GeoJsonLayer({
        id: "stops",
        data: visibleFeatures("stopsGeo"),
        pointType: "circle",
        pointRadiusUnits: "pixels",
        getPointRadius: (f) => (f.properties.core ? 3.5 : 2),
        getFillColor: (f) => (f.properties.core ? theme.hubStation : theme.station),
        pickable: true,
        updateTriggers: { getFillColor: state.theme },
      }),
      new deck.ScatterplotLayer({
        id: "vehicle-glow",
        data: visible,
        getPosition: (r) => r.pos,
        getFillColor: (r) => [...vehicleColor(r.data), 55],
        radiusUnits: "pixels",
        getRadius: (r) => agencyRadius(r.data) * 2.2,
        updateTriggers: { getPosition: t },
      }),
      new deck.ScatterplotLayer({
        id: "vehicles",
        data: visible,
        getPosition: (r) => r.pos,
        getFillColor: (r) => vehicleColor(r.data),
        getLineColor: [255, 255, 255, 230],
        stroked: true,
        lineWidthUnits: "pixels",
        getLineWidth: 1,
        radiusUnits: "pixels",
        getRadius: (r) => agencyRadius(r.data),
        pickable: true,
        updateTriggers: { getPosition: t },
      }),
      new deck.TextLayer({
        id: "vehicle-labels",
        data: map.getZoom() >= LABEL_MIN_ZOOM ? visible.filter((r) => r.data.number) : [],
        getPosition: (r) => r.pos,
        getText: (r) => r.data.number,
        getSize: 11,
        getColor: theme.label,
        getPixelOffset: (r) => [0, -(agencyRadius(r.data) + 9)],
        fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
        fontWeight: 600,
        fontSettings: { sdf: true },
        outlineWidth: 3,
        outlineColor: theme.labelOutline,
        updateTriggers: { getPosition: t },
      }),
    ],
  });
}

function vehicleColor(v) {
  const route = state.routeInfo.get(`${v.agency}:${v.route_id}`);
  return route ? route.color : hexToRgb(AGENCIES[v.agency]?.color ?? "#cccccc");
}

const agencyRadius = (v) => AGENCIES[v.agency]?.radius ?? 4;

/**
 * Paths to highlight for a vehicle: its trip's exact shape, else its whole route. Nothing when the
 * vehicle isn't actually on its trip's shape (FreeMotion): the feed's trip is then likely wrong.
 */
function highlightPaths(rec) {
  const v = rec.data;
  if (v.shape_id != null && !(rec.motion instanceof TrackMotion)) return [];
  const color = vehicleColor(v);
  const gi = v.shape_id != null ? state.shapes.ids[`${v.agency}:${v.shape_id}`] : undefined;
  if (gi !== undefined) return [{ path: state.shapes.geoms[gi], color }];
  const route = state.routes?.features.find((f) => f.properties.agency === v.agency && f.properties.route_id === v.route_id);
  return route ? route.geometry.coordinates.map((path) => ({ path, color })) : [];
}

// Static features minus hidden agencies; same array object until the toggles change, so deck.gl
// doesn't re-upload geometry every frame.
const featureCache = {};
function visibleFeatures(key) {
  const hiddenKey = [...state.hidden].sort().join();
  const c = featureCache[key];
  if (c && c.hiddenKey === hiddenKey && c.source === state[key]) return c.features;
  const features = (state[key]?.features ?? []).filter((f) => !state.hidden.has(f.properties.agency));
  featureCache[key] = { hiddenKey, source: state[key], features };
  return features;
}

function getTooltip({ object, layer }) {
  if (!object) return null;
  if (layer.id === "stops") return { text: object.properties.name };
  const v = object.data;
  const route = state.routeInfo.get(`${v.agency}:${v.route_id}`);
  const nextStop = v.next_stop_id && state.stops.get(`${v.agency}:${v.next_stop_id}`)?.name;
  const agency = AGENCIES[v.agency];
  const title = v.number ? `${agency?.kind ?? ""} ${v.number}`.trim() : agency?.label ?? v.agency;
  const lines = [
    `<b>${escapeHtml(title)}</b> · ${agency?.label ?? v.agency}`,
    route?.name && escapeHtml(route.name),
    v.headsign && `to ${escapeHtml(v.headsign)}`,
    v.vehicle_label && `${agency?.kind === "Bus" ? "Bus" : "Car"} #${escapeHtml(v.vehicle_label)}`,
    { inbound: "→ Manhattan", outbound: "Leaving Manhattan", through: "Through Manhattan" }[v.direction],
    nextStop && `Next: ${escapeHtml(nextStop)}${v.next_stop_eta ? ` at ${fmtTime(v.next_stop_eta)}` : ""}`,
    v.delay_s != null && fmtDelay(v.delay_s),
  ].filter(Boolean);
  return { html: lines.join("<br>") };
}

// ---------- panel ----------

function renderPanel() {
  const rows = Object.entries(AGENCIES).map(([id, a]) => {
    let inb = 0, out = 0, total = 0;
    for (const { data: v } of state.vehicles.values()) {
      if (v.agency !== id) continue;
      total++;
      if (v.direction === "inbound") inb++;
      if (v.direction === "outbound") out++;
    }
    const feed = state.feeds[id];
    const classes = [feed && !feed.ok && "error", state.hidden.has(id) && "off"].filter(Boolean).join(" ");
    const title = feed?.error ? escapeHtml(feed.error) : "Click to show/hide";
    return `<tr data-agency="${id}" class="${classes}" title="${title}">` +
      `<td><span class="swatch" style="background:${a.color}"></span>${a.label}</td>` +
      `<td>${inb}</td><td>${out}</td><td>${total}</td></tr>`;
  });
  document.querySelector("#counts tbody").innerHTML = rows.join("");
  updateStatus();
}

function updateStatus() {
  const times = Object.values(state.lastUpdate);
  if (!times.length) return;
  const failed = Object.entries(state.feeds).filter(([, f]) => f && !f.ok).map(([id]) => AGENCIES[id]?.label ?? id);
  const age = Math.round((Date.now() - Math.max(...times)) / 1000);
  setStatus(`Updated ${age}s ago${failed.length ? ` · feed problem: ${failed.join(", ")}` : ""}`, failed.length > 0);
}

document.querySelector("#counts tbody").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-agency]");
  if (!row) return;
  const id = row.dataset.agency;
  if (state.hidden.has(id)) state.hidden.delete(id); else state.hidden.add(id);
  renderPanel();
});

function setStatus(text, isError = false) {
  const el = document.getElementById("status");
  el.textContent = text;
  el.classList.toggle("error", isError);
}

// ---------- theme ----------

function applyTheme(theme, { save }) {
  if (theme === state.theme && map.loaded()) return;
  state.theme = theme;
  document.documentElement.dataset.theme = theme;
  if (save) {
    try {
      localStorage.setItem("theme", theme);
    } catch {
      // storage blocked: the toggle still works for this visit
    }
  }
  map.setStyle(THEMES[theme].basemap); // deck.gl layers sit in their own canvas and stay put
  updateThemeButton();
}

function updateThemeButton() {
  const next = state.theme === "dark" ? "light" : "dark";
  const btn = document.getElementById("theme-toggle");
  btn.setAttribute("aria-label", `Switch to ${next} mode`);
  btn.title = `Switch to ${next} mode`;
}

document.getElementById("theme-toggle").addEventListener("click", () => {
  applyTheme(state.theme === "dark" ? "light" : "dark", { save: true });
});
// Follow the system setting until the user picks a theme with the toggle.
window.matchMedia?.("(prefers-color-scheme: light)").addEventListener?.("change", () => {
  if (!savedTheme()) applyTheme(systemTheme(), { save: false });
});
updateThemeButton();

document.querySelector(".filters").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  state.dir = btn.dataset.dir;
  document.querySelectorAll(".filters button").forEach((b) => b.classList.toggle("active", b === btn));
});

// ---------- utils ----------

function hexToRgb(hex) {
  const n = parseInt(hex.replace("#", ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
const fmtTime = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const fmtDelay = (s) => (Math.abs(s) < 60 ? "On time" : s > 0 ? `${Math.round(s / 60)} min late` : `${Math.round(-s / 60)} min early`);
const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// ---------- boot ----------

let feedsConfig = [];  // [{id, poll_s}] from /api/agencies
let timers = [];
function schedule() {
  timers.forEach(clearInterval);
  timers = [];
  if (document.hidden) return;
  for (const { id, poll_s } of feedsConfig) {
    poll(id);
    timers.push(setInterval(() => poll(id), poll_s * 1000));
  }
}
document.addEventListener("visibilitychange", schedule);
setInterval(updateStatus, 1000);

map.on("load", async () => {
  const [, agencies] = await Promise.all([syncStatic(), fetch("api/agencies").then((r) => r.json())]);
  setInterval(syncStatic, STATIC_CHECK_MS);
  feedsConfig = agencies;
  applyRegionLimits();
  renderPanel();
  schedule();
  requestAnimationFrame(frame);
});
