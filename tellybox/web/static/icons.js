// Inline SVG icons: chunky rounded strokes in ink (currentColor). No text anywhere (KA-2).

const svg = (body, vb = "0 0 48 48", cls = "icon") =>
  `<svg class="${cls}" viewBox="${vb}" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;

export const icons = {
  home: svg(`<path d="M8 23 24 9l16 14"/><path d="M13 20v18h22V20"/><path d="M21 38v-9h6v9"/>`),

  play: svg(`<path d="M17 11.5v25a1.5 1.5 0 0 0 2.3 1.3l19-12.5a1.5 1.5 0 0 0 0-2.6l-19-12.5A1.5 1.5 0 0 0 17 11.5Z" fill="currentColor"/>`),

  pause: svg(`<rect x="12" y="10" width="8" height="28" rx="3" fill="currentColor"/><rect x="28" y="10" width="8" height="28" rx="3" fill="currentColor"/>`),

  // Small ring drawn around the big button while loading/buffering or a request is in flight.
  spinner: `<svg class="spinner" viewBox="0 0 100 100" aria-hidden="true" focusable="false"><circle cx="50" cy="50" r="46" fill="none" stroke="currentColor" stroke-width="6" stroke-linecap="round" stroke-dasharray="70 220"/></svg>`,

  // TV showing a cross, its plug hanging loose: the TV can't be reached.
  tvOffline: svg(
    `<path d="m14 3 6 5 6-5"/><rect x="3" y="8" width="34" height="24" rx="6"/>` +
      `<path d="m15 15 10 10M25 15 15 25"/>` +
      `<path d="M20 32v5a4 4 0 0 0 4 4h5"/><rect x="29" y="36" width="9" height="10" rx="2.5"/><path d="M38 38.5h5M38 43.5h5"/>`,
    "0 0 48 48",
  ),

  // Sleepy TV: nothing playing.
  tvSleepy: svg(
    `<rect x="6" y="12" width="36" height="26" rx="6"/><path d="m17 5 7 7 7-7"/>` +
      `<path d="M14 25q3.5 3 7 0M27 25q3.5 3 7 0"/><path d="M16 42h16"/>`,
  ),

  // Plain TV and phone: the two places a pick can play (KA-13).
  tv: svg(`<path d="m16 5 8 7 8-7"/><rect x="5" y="12" width="38" height="27" rx="6"/><path d="M16 44h16"/>`),
  phone: svg(`<rect x="12" y="4" width="24" height="40" rx="6"/><path d="M21 37h6"/>`),

  // Cross: leave the in-app player.
  close: svg(`<path d="m12 12 24 24M36 12 12 36"/>`),

  // Sad cloud with a tear: the video can't play. No text (KA-2).
  sadCloud: svg(
    `<path d="M14 34a8 8 0 0 1-1-15.9A11 11 0 0 1 34 15a9.5 9.5 0 0 1 1 19Z"/>` +
      `<path d="M19 27h.1M29 27h.1"/><path d="M19 31.5q5-3.5 10 0"/><path d="M33 38q-2.5 3 0 5.5t0-5.5Z" fill="currentColor"/>`,
  ),

  // Big arrow for the "go" button on the who's-watching screen.
  go: svg(`<path d="M9 24h29"/><path d="m26 11 13 13-13 13"/>`),

  // Check on a green disc: this kid is picked.
  check: `<svg class="badge-svg" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><circle cx="24" cy="24" r="20" fill="#4CC36B" stroke="#1E2A5A" stroke-width="4.5"/><path d="m14 25 7 7 13-15" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

  // Small moon on a night-blue disc: this kid's time is up for today.
  moonBadge: `<svg class="badge-svg" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><circle cx="24" cy="24" r="20" fill="#232B63" stroke="#FFF3B0" stroke-width="3.5"/><path d="M27 12a12.5 12.5 0 1 0 8 19 10.5 10.5 0 0 1-8-19Z" fill="#FFF3B0"/></svg>`,

  star: `<svg class="badge-svg" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><path d="M24 5.5l5.4 11.2 12.3 1.6-9 8.6 2.3 12.2L24 33.3l-11 5.8 2.3-12.2-9-8.6 12.3-1.6Z" fill="#FFC93C" stroke="#1E2A5A" stroke-width="4.5" stroke-linejoin="round"/></svg>`,

  next: `<svg class="badge-svg" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><circle cx="24" cy="24" r="19" fill="#FFC93C" stroke="#1E2A5A" stroke-width="4.5"/><path d="M20 15.5v17l13-8.5Z" fill="#1E2A5A" stroke="#1E2A5A" stroke-width="3" stroke-linejoin="round"/></svg>`,
};

// Picture for a profile without a photo or a built-in avatar: a friendly face in a colour.
export function placeholderFace(color) {
  return `<svg class="placeholder" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
    <rect width="100" height="100" fill="${color}"/>
    <circle cx="50" cy="52" r="30" fill="#fff" fill-opacity=".6"/>
    <g fill="#1E2A5A"><circle cx="39" cy="47" r="4.5"/><circle cx="61" cy="47" r="4.5"/></g>
    <path d="M38 61q12 11 24 0" fill="none" stroke="#1E2A5A" stroke-width="4.5" stroke-linecap="round"/>
  </svg>`;
}

// Placeholder TV in the show's colour, drawn when an image is missing.
export function placeholderTv(color) {
  return `<svg class="placeholder" viewBox="0 0 160 90" preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false">
    <rect width="160" height="90" fill="${color}" opacity=".35"/>
    <g fill="none" stroke="#1E2A5A" stroke-width="5" stroke-linecap="round" stroke-linejoin="round">
      <path d="m68 16 12 10 12-10"/>
      <rect x="50" y="26" width="60" height="42" rx="9" fill="${color}"/>
      <rect x="59" y="34" width="42" height="26" rx="5" fill="#fff" fill-opacity=".55" stroke-width="4"/>
      <path d="M66 76h28"/>
    </g>
  </svg>`;
}
