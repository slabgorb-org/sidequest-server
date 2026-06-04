/* Reference picker hydration. Bound to elements with data-island="picker".
 * Each picker has .ref-picker__chip buttons (data-target="<panel-id>") and
 * .ref-picker-panel sections (data-panel="<panel-id>"). Click a chip to
 * swap which panel is visible. Works statically without JS — default chip
 * has aria-selected="true" server-side and default panel is unhidden.
 *
 * Budget: ≤4KB. Vanilla JS, no deps. */
(function () {
  function bindPicker(root) {
    var chips = root.querySelectorAll(".ref-picker__chip");
    var panels = root.querySelectorAll(".ref-picker-panel");
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var target = chip.getAttribute("data-target");
        chips.forEach(function (c) {
          c.setAttribute("aria-selected", c === chip ? "true" : "false");
        });
        panels.forEach(function (p) {
          if (p.getAttribute("data-panel") === target) {
            p.removeAttribute("hidden");
          } else {
            p.setAttribute("hidden", "");
          }
        });
      });
    });
    // URL hash deep-link: if window.location.hash matches a panel, activate it.
    var hash = window.location.hash.slice(1);
    if (hash) {
      var match = root.querySelector('.ref-picker__chip[data-target="' + hash + '"]');
      if (match) match.click();
    }
  }
  document.querySelectorAll('[data-island="picker"]').forEach(bindPicker);
})();

/* Lore-image lightbox (Fix #5). Generic across every genre/world lore page:
 * clicking a cast portrait (img.ref-card__portrait) or POI landscape
 * (img.ref-card__poi) opens the image enlarged in an overlay. Dismiss via the
 * close button, Esc, or a backdrop click. Degrades gracefully when no images
 * are present (the click listener simply never fires). Vanilla JS, no deps. */
(function () {
  var SELECTOR = "img.ref-card__portrait, img.ref-card__poi";
  var overlay = null;
  var lastFocus = null;

  function close() {
    if (!overlay) return;
    overlay.remove();
    overlay = null;
    document.removeEventListener("keydown", onKey);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
  }

  function onKey(e) {
    if (e.key === "Escape") close();
  }

  function open(src, alt) {
    if (overlay) close();
    lastFocus = document.activeElement;
    overlay = document.createElement("div");
    overlay.className = "ref-lightbox";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ref-lightbox__close";
    btn.setAttribute("aria-label", "Close");
    btn.textContent = "×";
    btn.addEventListener("click", close);

    var img = document.createElement("img");
    img.className = "ref-lightbox__img";
    img.src = src;
    img.alt = alt || "";

    overlay.appendChild(btn);
    overlay.appendChild(img);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close();
    });
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey);
    btn.focus();
  }

  document.addEventListener("click", function (e) {
    var img = e.target.closest ? e.target.closest(SELECTOR) : null;
    if (img) open(img.getAttribute("src"), img.getAttribute("alt"));
  });
})();
