// Every admin page: the Inbox badge in the nav (CS-8) and a few form conveniences. Everything works without JS.
//
// 1. The pending count is polled while the page is visible, so a new upload shows up without a reload.
// 2. forms and buttons with data-confirm ask first (removing a subscription, rejecting everything).
// 3. A select with data-autosubmit submits its form on change (the inbox's channel filter).
// 4. .select-tools in a form offer "select all / none" for its checkboxes.

const POLL_MS = 5000;

const badge = document.getElementById("inbox-badge");
if (badge) {
  const show = (n) => {
    badge.textContent = String(n);
    badge.hidden = !n;
  };
  const poll = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch("/admin/inbox/count", { headers: { Accept: "application/json" } });
      if (response.ok) show((await response.json()).pending);
    } catch {
      // offline or restarting: keep the last count
    }
  };
  setInterval(poll, POLL_MS);
  document.addEventListener("visibilitychange", poll);
}

document.addEventListener("submit", (event) => {
  const submitter = event.submitter;
  const message = submitter?.dataset.confirm || event.target.dataset?.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

for (const select of document.querySelectorAll("select[data-autosubmit]")) {
  select.addEventListener("change", () => select.form.submit());
}

for (const tools of document.querySelectorAll("form .select-tools")) {
  const form = tools.closest("form");
  const boxes = () => [...form.querySelectorAll('input[type="checkbox"][name="item"], input[type="checkbox"][name="video"]')];
  if (boxes().length < 2) continue;
  tools.hidden = false;
  tools.addEventListener("click", (event) => {
    const which = event.target.closest("[data-select]");
    if (!which) return;
    for (const box of boxes().filter((b) => !b.disabled)) box.checked = which.dataset.select === "all";
    form.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
