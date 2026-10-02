// Show the minutes input only when a limit is set to "Custom" (A-23). Without JS every input stays visible.
for (const select of document.querySelectorAll(".allowance-mode, .max-session-mode")) {
  const kind = select.classList.contains("allowance-mode") ? "allowance" : "session";
  const label = document.querySelector(`label.custom-input[data-pid="${select.dataset.pid}"][data-mode="${kind}"]`);
  if (!label) continue;
  const toggle = () => { label.hidden = select.value !== "custom"; };
  select.addEventListener("change", toggle);
  toggle();
}
