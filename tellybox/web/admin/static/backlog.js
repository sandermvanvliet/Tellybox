// Backlog of a new subscription (CS-1): "Load more" appends the next page to the same form, so ticks on the
// earlier pages survive; the submit button counts the ticks. Without JS "Load more" is a plain link to the next page.

import { tn } from "./i18n.js";

const form = document.getElementById("backlog-form");
if (form) {
  const submit = document.getElementById("backlog-submit");
  const list = document.getElementById("backlog-list");
  const update = () => {
    const n = form.querySelectorAll('input[name="video"]:checked').length;
    submit.textContent = tn("Add %(num)d video", "Add %(num)d videos", n);
    submit.disabled = n === 0;
  };
  form.addEventListener("change", update);
  form.addEventListener("submit", () => setTimeout(() => { submit.disabled = true; }));

  const more = document.getElementById("load-more");
  if (more) {
    more.addEventListener("click", async (event) => {
      event.preventDefault();
      more.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(`${more.getAttribute("href")}&fragment=1`);
        if (!response.ok) throw new Error(String(response.status));
        list.insertAdjacentHTML("beforeend", await response.text());
        const next = response.headers.get("X-Next-Offset");
        if (next) {
          const url = new URL(more.getAttribute("href"), location.href);
          url.searchParams.set("offset", next);
          more.setAttribute("href", url.search);
        } else {
          more.parentElement.remove();
        }
        update();
      } catch {
        location.href = more.getAttribute("href"); // fall back to the plain page
      } finally {
        more.removeAttribute("aria-busy");
      }
    });
  }
  update();
}
