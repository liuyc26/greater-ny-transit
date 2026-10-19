// Moving vehicles along their track.
//
// Rail feeds report positions coarsely (stepwise, sometimes minutes old), so we don't draw the
// raw position. Each report is snapped onto the trip's shape and becomes a plan: "at distance d0
// at time t0, reach distance d1 at time t1". d1/t1 come from the next stop and its ETA when the
// feed has one (MTA), otherwise from the speed measured between reports (NJT), capped at the
// next stop and a short horizon. Every frame the dot eases toward where the plan says it should
// be now, so it keeps moving between reports and never teleports.

const M_PER_DEG_LAT = 111_320;
const M_PER_DEG_LON = M_PER_DEG_LAT * Math.cos((40.75 * Math.PI) / 180);

const MAX_TRACK_OFFSET_M = 500; // a report farther than this from the track isn't on it
const TELEPORT_M = 4000;        // jump instead of gliding when this far off
const BACKTRACK_M = 400;        // only glide backwards when the plan is this far behind
const CATCHUP_TAU_S = 1.5;      // easing time constant toward the plan
const MAX_SPEED_MPS = 45;       // ~100 mph; caps implausible plans
const MIN_SPEED_MPS = 0.5;      // below this, treat as stopped
const EXTRAPOLATE_S = 60;       // without an ETA, dead-reckon at most this far past a report
const HINT_WINDOW_M = [-1500, 4000]; // search near the last position first (loops, out-and-back routes)
const HINT_ACCEPT_M = 120;

export class Track {
  constructor(coords) {
    const n = coords.length;
    this.coords = coords;
    this.xs = new Float64Array(n);
    this.ys = new Float64Array(n);
    this.cum = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      this.xs[i] = coords[i][0] * M_PER_DEG_LON;
      this.ys[i] = coords[i][1] * M_PER_DEG_LAT;
      if (i > 0) this.cum[i] = this.cum[i - 1] + Math.hypot(this.xs[i] - this.xs[i - 1], this.ys[i] - this.ys[i - 1]);
    }
    this.length = this.cum[n - 1];
  }

  /**
   * Nearest point on the track: {d: meters along, offset: meters away}. With a hint (a previous
   * distance along), prefer a close match near it, so a route that doubles back on itself doesn't
   * snap to the wrong leg.
   */
  snap(point, hint = null) {
    if (hint !== null) {
      const near = this._snapRange(point, hint + HINT_WINDOW_M[0], hint + HINT_WINDOW_M[1]);
      if (near.offset <= HINT_ACCEPT_M) return near;
    }
    return this._snapRange(point, -Infinity, Infinity);
  }

  _snapRange([lon, lat], dMin, dMax) {
    const px = lon * M_PER_DEG_LON, py = lat * M_PER_DEG_LAT;
    let best = { d: 0, offset: Infinity };
    for (let i = 0; i < this.xs.length - 1; i++) {
      if (this.cum[i + 1] < dMin || this.cum[i] > dMax) continue;
      const ax = this.xs[i], ay = this.ys[i];
      const dx = this.xs[i + 1] - ax, dy = this.ys[i + 1] - ay;
      const len2 = dx * dx + dy * dy;
      const t = len2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0;
      const off = Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
      if (off < best.offset) best = { d: this.cum[i] + t * Math.sqrt(len2), offset: off };
    }
    return best;
  }

  /** [lon, lat] at distance d along the track. */
  pointAt(d) {
    const cum = this.cum;
    if (d <= 0) return this.coords[0];
    if (d >= this.length) return this.coords[this.coords.length - 1];
    let lo = 0, hi = cum.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (cum[mid] <= d) lo = mid; else hi = mid;
    }
    const t = (d - cum[lo]) / (cum[hi] - cum[lo] || 1);
    const a = this.coords[lo], b = this.coords[hi];
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  }
}

/** Per-vehicle motion along a Track. */
export class TrackMotion {
  constructor(track) {
    this.track = track;
    this.plan = null;
    this.d = null;
    this.lastStep = null;
    this.lastObs = null; // {d, t} of the previous distinct report
    this.speed = null;   // m/s along the track, smoothed
  }

  /**
   * Update the plan from a vehicle report. Returns false if the report isn't on this track
   * (caller should fall back to raw positions).
   */
  observe(v, nextStopCoord, nowS) {
    const here = this.track.snap([v.lon, v.lat], this.lastObs?.d ?? null);
    if (here.offset > MAX_TRACK_OFFSET_M) return false;

    // Measured speed between distinct reports (feeds often repeat the same report).
    const prev = this.lastObs;
    if (prev && v.observed_at > prev.t) {
      const s = (here.d - prev.d) / (v.observed_at - prev.t);
      if (s >= 0 && s <= MAX_SPEED_MPS) this.speed = this.speed === null ? s : 0.5 * this.speed + 0.5 * s;
    }
    if (!prev || v.observed_at !== prev.t) this.lastObs = { d: here.d, t: v.observed_at };

    let next = null;
    if (nextStopCoord) {
      const s = this.track.snap(nextStopCoord, here.d);
      if (s.offset <= MAX_TRACK_OFFSET_M && s.d > here.d) next = s;
    }

    // Dwelling at a station: stay put until the scheduled departure.
    const t0 = Math.max(v.observed_at, v.hold_until ?? 0);
    let plan = { d0: here.d, t0, d1: here.d, t1: t0 };
    if (next && v.next_stop_eta > t0) {
      const minDuration = (next.d - here.d) / MAX_SPEED_MPS;
      plan = { d0: here.d, t0, d1: next.d, t1: Math.max(v.next_stop_eta, t0 + minDuration) };
    } else if (this.speed > MIN_SPEED_MPS) {
      const d1 = Math.min(next ? next.d : Infinity, here.d + this.speed * EXTRAPOLATE_S, this.track.length);
      plan = { d0: here.d, t0, d1, t1: t0 + (d1 - here.d) / this.speed };
    }
    this.plan = plan;
    if (this.d === null) this.d = this.target(nowS);
    return true;
  }

  target(t) {
    const p = this.plan;
    if (p.t1 <= p.t0) return p.d0;
    const k = Math.max(0, Math.min(1, (t - p.t0) / (p.t1 - p.t0)));
    return p.d0 + (p.d1 - p.d0) * k;
  }

  /** Advance to time nowS and return [lon, lat]. */
  step(nowS) {
    const dt = this.lastStep === null ? 0 : Math.min(1, Math.max(0, nowS - this.lastStep));
    this.lastStep = nowS;
    const diff = this.target(nowS) - this.d;
    if (Math.abs(diff) > TELEPORT_M) this.d += diff;
    else if (diff > 0 || diff < -BACKTRACK_M) this.d += diff * (1 - Math.exp(-dt / CATCHUP_TAU_S));
    // small negative diff: plan is slightly behind us, so hold still instead of reversing
    return this.track.pointAt(this.d);
  }
}

/** Fallback for vehicles without a usable track: ease between raw positions. */
export class FreeMotion {
  constructor(lon, lat) {
    this.pos = [lon, lat];
    this.lastStep = null;
  }

  observe(v) {
    this.goal = [v.lon, v.lat];
    return true;
  }

  step(nowS) {
    const dt = this.lastStep === null ? 1 : Math.min(1, Math.max(0, nowS - this.lastStep));
    this.lastStep = nowS;
    const k = 1 - Math.exp(-dt / CATCHUP_TAU_S);
    this.pos = [this.pos[0] + (this.goal[0] - this.pos[0]) * k, this.pos[1] + (this.goal[1] - this.pos[1]) * k];
    return this.pos;
  }
}
