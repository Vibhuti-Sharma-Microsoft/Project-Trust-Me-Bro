"use strict";

(() => {
  const selector = document.getElementById("incident-select");
  const host = document.getElementById("scorecard-host");
  if (!selector || !host) return;

  const dialog = document.getElementById("dimension-dialog");
  const title = document.getElementById("dimension-title");
  const content = document.getElementById("dimension-content");
  const incident = document.getElementById("dialog-incident");
  const closeButton = document.getElementById("close-dimension");
  const status = document.getElementById("selection-status");
  const cases = new Map(
    Array.from(document.querySelectorAll("template[data-case-template]"), (template) => [
      template.dataset.caseTemplate, template
    ])
  );
  let activeTile = null;

  function closeDetail(restoreFocus = true) {
    const previous = activeTile;
    activeTile = null;
    if (dialog.open) dialog.close();
    content.replaceChildren();
    title.textContent = "";
    incident.textContent = "";
    if (previous) {
      previous.setAttribute("aria-expanded", "false");
      if (restoreFocus && previous.isConnected) previous.focus();
    }
  }

  selector.addEventListener("change", () => {
    const template = cases.get(selector.value);
    if (!template) throw new Error("Selected incident scorecard is missing.");
    closeDetail(false);
    host.replaceChildren(template.content.cloneNode(true));
    const selectedTitle = host.querySelector("#incident-title").textContent;
    document.title = `Scoring Service | ${selectedTitle}`;
    status.textContent = `${selectedTitle}: ${host.querySelector("[data-status]").textContent}, score ${host.querySelector('[data-score="total"]').textContent}.`;
    selector.focus();
  });

  host.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const tile = event.target.closest("button[data-dimension]");
    if (!tile || !host.contains(tile)) return;
    const detail = Array.from(host.querySelectorAll("template[data-dimension-detail]"))
      .find((template) => template.dataset.dimensionDetail === tile.dataset.dimension);
    if (!detail) throw new Error("Dimension explanation is missing.");
    closeDetail(false);
    activeTile = tile;
    title.textContent = `${tile.querySelector(".dimension-name").textContent} contribution`;
    incident.textContent = host.querySelector("#incident-title").textContent;
    // Only clone autoescaped server-rendered templates; never interpret data as HTML.
    content.replaceChildren(detail.content.cloneNode(true));
    tile.setAttribute("aria-expanded", "true");
    dialog.showModal();
    closeButton.focus();
  });

  closeButton.addEventListener("click", () => closeDetail());
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeDetail();
  });
})();
