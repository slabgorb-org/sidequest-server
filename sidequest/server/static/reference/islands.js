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
