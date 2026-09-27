// Améliorations progressives : partage natif, menu des jeux et service worker.
(function () {
  "use strict";
  document.querySelectorAll("[data-share]").forEach(function (btn) {
    if (!navigator.share) return;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      navigator.share({ title: document.title, url: location.href }).catch(function () {});
    });
  });
  // Menu des jeux : ouvre le jeu dès qu'il est choisi (le bouton « Voir » sert sans JS).
  document.querySelectorAll("[data-autosubmit]").forEach(function (select) {
    var fallback = select.form.querySelector("[data-autosubmit-fallback]");
    if (fallback) fallback.hidden = true;
    select.addEventListener("change", function () { select.form.submit(); });
  });
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }
})();
