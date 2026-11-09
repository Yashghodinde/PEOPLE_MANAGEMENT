document.addEventListener("DOMContentLoaded", function () {
  [["sg-toast-ok", "success", "Saved."], ["sg-toast-warn", "warning", "Offline: 3 changes waiting."], ["sg-toast-err", "error", "Could not save. Try again."]].forEach(function (t) {
    var b = document.getElementById(t[0]);
    if (b) b.addEventListener("click", function () { window.otsToast(t[1], t[2]); });
  });
});
