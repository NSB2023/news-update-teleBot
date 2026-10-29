const domains = [...document.querySelectorAll(".domain-choice")];
const model = document.getElementById("preview-model");
const timezone = document.getElementById("preview-timezone");
const time = document.getElementById("preview-time");
const duration = document.getElementById("preview-duration");
const durationValue = document.getElementById("preview-duration-value");
const readout = document.getElementById("preview-readout");

function updatePreview() {
  const selected = domains.filter(button => button.getAttribute("aria-pressed") === "true")
    .map(button => button.dataset.domain);
  durationValue.value = `${duration.value} min`;
  readout.textContent = `${selected.join(" · ")}  |  ${model.value}  |  ${time.value} ${timezone.value}  |  about ${duration.value} minutes`;
}

for (const button of domains) {
  button.addEventListener("click", () => {
    const active = button.getAttribute("aria-pressed") === "true";
    if (active && domains.filter(item => item.getAttribute("aria-pressed") === "true").length === 1) return;
    button.setAttribute("aria-pressed", String(!active));
    updatePreview();
  });
}

for (const control of [model, timezone, time, duration]) control.addEventListener("input", updatePreview);
updatePreview();
