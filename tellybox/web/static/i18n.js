// Interface languages (NF-13) for the kid app. It shows no text by default (KA-2); the reader UI (KA-11) adds visible text. Otherwise only the
// screen-reader labels and <html lang> follow the browser's language. The page is static,
// so the language comes from navigator.languages with the same rule as the server
// (tellybox.i18n.negotiate): the first supported primary subtag, else English.
// tests/web/test_kid_i18n.py checks that every dictionary has the same keys.

const LABELS = {
  en: {
    "Time left": "Time left",
    "Time's up for today": "Time's up for today",
    "No time limit": "No time limit",
    "%(pct)s% of today's time left": "%(pct)s% of today's time left",
    "%(pct)s% of today's time left, almost done": "%(pct)s% of today's time left, almost done",
    "Keep watching": "Keep watching",
    "Shows": "Shows",
    "Home": "Home",
    "Pause": "Pause",
    "Play": "Play",
    "Nothing playing": "Nothing playing",
    "TV not reachable": "TV not reachable",
    "Who's watching?": "Who's watching?",
    "Go": "Go",
    "Change who's watching": "Change who's watching",
    "Playing": "Playing",
    "Paused": "Paused",
    "Loading": "Loading",
    "on %(tv)s": "on %(tv)s",
    "Search shows and episodes": "Search shows and episodes",
    "No matches": "No matches",
    "Change": "Change",
  },
  nl: {
    "Time left": "Tijd over",
    "Time's up for today": "De tijd is op voor vandaag",
    "No time limit": "Geen tijdslimiet",
    "%(pct)s% of today's time left": "Nog %(pct)s% van de tijd over vandaag",
    "%(pct)s% of today's time left, almost done": "Nog %(pct)s% van de tijd over vandaag, bijna klaar",
    "Keep watching": "Verder kijken",
    "Shows": "Series",
    "Home": "Start",
    "Pause": "Pauzeren",
    "Play": "Afspelen",
    "Nothing playing": "Er speelt niets",
    "TV not reachable": "Tv niet bereikbaar",
    "Who's watching?": "Wie kijkt er?",
    "Go": "Start",
    "Change who's watching": "Wijzig wie er kijkt",
    "Playing": "Speelt",
    "Paused": "Gepauzeerd",
    "Loading": "Laden",
    "on %(tv)s": "op %(tv)s",
    "Search shows and episodes": "Zoek series en afleveringen",
    "No matches": "Niets gevonden",
    "Change": "Wijzig",
  },
  de: {
    "Time left": "Verbleibende Zeit",
    "Time's up for today": "Die Zeit für heute ist um",
    "No time limit": "Kein Zeitlimit",
    "%(pct)s% of today's time left": "Noch %(pct)s% der Zeit für heute",
    "%(pct)s% of today's time left, almost done": "Noch %(pct)s% der Zeit für heute, fast vorbei",
    "Keep watching": "Weiterschauen",
    "Shows": "Sendungen",
    "Home": "Start",
    "Pause": "Pause",
    "Play": "Abspielen",
    "Nothing playing": "Es läuft nichts",
    "TV not reachable": "Fernseher nicht erreichbar",
    "Who's watching?": "Wer schaut zu?",
    "Go": "Los",
    "Change who's watching": "Ändern, wer zuschaut",
    "Playing": "Läuft",
    "Paused": "Pausiert",
    "Loading": "Lädt",
    "on %(tv)s": "auf %(tv)s",
    "Search shows and episodes": "Sendungen und Folgen suchen",
    "No matches": "Keine Treffer",
    "Change": "Ändern",
  },
};

export function negotiate(preferred) {
  for (const tag of preferred || []) {
    const primary = String(tag).toLowerCase().split("-")[0];
    if (primary in LABELS) return primary;
  }
  return "en";
}

export const lang = negotiate(navigator.languages?.length ? navigator.languages : [navigator.language]);

// A label in the kid's language; %(name)s placeholders are filled from `vars`.
export function label(key, vars) {
  const text = LABELS[lang][key] ?? key;
  return text.replace(/%\((\w+)\)s/g, (match, name) => (vars && name in vars ? String(vars[name]) : match));
}

// Set <html lang> and translate the aria-labels written in index.html.
export function translatePage(root = document) {
  document.documentElement.lang = lang;
  for (const node of root.querySelectorAll("[aria-label]")) {
    node.setAttribute("aria-label", label(node.getAttribute("aria-label")));
  }
}
