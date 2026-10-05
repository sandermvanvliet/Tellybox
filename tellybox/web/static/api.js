// Kid API client (docs/kid-api.md).

export class HttpError extends Error {
  constructor(status) {
    super(`HTTP ${status}`);
    this.status = status;
  }
}

export async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
  if (!r.ok) throw new HttpError(r.status);
  return r.json();
}

// Returns {status, data}; a network failure is status 0 (treated like an unreachable TV).
export async function postJSON(url, body) {
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await r.json();
    } catch {
      /* empty or non-JSON body */
    }
    return { status: r.status, data };
  } catch {
    return { status: 0, data: null };
  }
}

// `ids` is the device's group (PR-2): the profiles that are watching, passed on every call.
const group = (ids) => (ids && ids.length ? `?profiles=${ids.map(encodeURIComponent).join(",")}` : "");

export const api = {
  profiles: () => getJSON("/api/kid/profiles"),
  home: (ids) => getJSON(`/api/kid/home${group(ids)}`),
  show: (id, ids) => getJSON(`/api/kid/shows/${encodeURIComponent(id)}${group(ids)}`),
  state: () => getJSON("/api/kid/state"),
  // `device` ({target: "device", device_id}) plays in the app (KA-14); without it the body is the TV one, unchanged.
  play: (episodeId, ids, device) => postJSON("/api/kid/play", { episode_id: episodeId, profile_ids: ids, ...(device || {}) }),
  pause: () => postJSON("/api/kid/pause"),
  resume: () => postJSON("/api/kid/resume"),
};

// Live KidState stream. EventSource reconnects by itself after a dropped connection;
// if it gives up (readyState CLOSED, e.g. a 502 from a proxy) we reconnect with backoff.
export function subscribe({ onState, onDown }) {
  let es = null;
  let delay = 1000;
  let timer = null;

  const connect = () => {
    timer = null;
    es = new EventSource("/api/kid/events");
    es.onmessage = (ev) => {
      delay = 1000;
      let s;
      try {
        s = JSON.parse(ev.data);
      } catch {
        return;
      }
      onState(s);
    };
    es.onerror = () => {
      onDown();
      if (es.readyState === EventSource.CLOSED && !timer) {
        es.close();
        timer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 15000);
      }
    };
  };
  connect();
}

// KA-15: a successful home answer with nothing in it means no shows are visible to this profile or
// group. Loading and errors never reach the renderer, so this is not confused with them.
export function isEmptyHome(data) {
  return !((data?.shows && data.shows.length) || (data?.continue && data.continue.length));
}
