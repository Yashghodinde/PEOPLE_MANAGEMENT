/* Selfie step of login (M06.1, rebuilt IMP-16.5). States: asking -> live -> captured -> sending -> done, with
   failed (retry), denied, nocamera, busy and insecure on the side. The picture is shown before it is sent and
   can be retaken. One idempotency key belongs to one picture, so sending the same picture again can never make a
   second session; a retake is a new picture and a new key. A camera that cannot be used is reported to the
   server once, with the reason; login stays closed until a picture is accepted. */
(function () {
  "use strict";
  var root = document.getElementById("selfie-root");
  if (!root) return;
  var video = document.getElementById("selfie-video"), canvas = document.getElementById("selfie-canvas");
  var still = document.getElementById("selfie-still"), msg = document.getElementById("selfie-msg");
  var url = root.getAttribute("data-url"), csrf = root.getAttribute("data-csrf");
  var stream = null, blob = null, key = null, reported = false, attempt = 0, objectUrl = "";

  var TEXT = {
    asking: "Allow camera access when your browser asks.",
    live: "Look at the camera, then press Take picture.",
    captured: "Is this picture clear? Use it, or retake it.",
    sending: "Sending your picture...",
    denied: "Camera access is blocked. Allow it for this site in your browser settings, then press Try again.",
    nocamera: "No camera was found on this computer. Connect one, then press Try again.",
    busy: "The camera is being used by another program. Close it, then press Try again.",
    insecure: "The camera works only on a secure (https) page. Ask your administrator to open the system with https.",
    failed: "The picture could not be sent. Check the connection, then press Send again.",
  };

  function uuid() { return (window.otsUuid7 ? window.otsUuid7() : crypto.randomUUID()); }
  function deviceId() {
    try { var d = localStorage.getItem("ots-device"); if (!d) { d = uuid(); localStorage.setItem("ots-device", d); } return d; }
    catch (e) { return ""; }
  }
  function setState(name, text) {
    root.setAttribute("data-state", name);
    msg.textContent = text || TEXT[name] || "";
    msg.className = "text-body " + (/^(denied|nocamera|busy|insecure|failed)$/.test(name) ? "text-bad" : "text-ink-900");
    Array.prototype.forEach.call(root.querySelectorAll("[data-show]"), function (el) {
      var on = el.getAttribute("data-show").split(" ").indexOf(name) !== -1;
      if (el.tagName === "IMG" || el.tagName === "VIDEO") el.hidden = !on; else el.style.display = on ? "" : "none";
    });
  }
  function post(form, k) {
    return fetch(url, { method: "POST", body: form, credentials: "same-origin", headers: { "Idempotency-Key": k, "X-CSRFToken": csrf } });
  }
  // Tell the server why there is no picture, once per page. It records the reason; it never lets anyone in.
  function report(reason) {
    if (reported) return; reported = true;
    var f = new FormData(); f.append("camera", "unavailable"); f.append("reason", reason);
    post(f, uuid()).catch(function () {});
  }
  function stopStream() { if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; } video.srcObject = null; }
  function reasonOf(err) {
    var n = err && err.name;
    if (n === "NotAllowedError" || n === "SecurityError" || n === "PermissionDeniedError") return "denied";
    if (n === "NotFoundError" || n === "DevicesNotFoundError" || n === "OverconstrainedError") return "nocamera";
    if (n === "NotReadableError" || n === "TrackStartError" || n === "AbortError") return "busy";
    return "error";
  }

  function start() {
    blob = null; key = null; attempt = 0;
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      var secure = !window.isSecureContext;
      setState(secure ? "insecure" : "nocamera"); report(secure ? "insecure" : "nocamera"); return;
    }
    setState("asking");
    navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false }).then(function (s) {
      stream = s; video.srcObject = s; setState("live");
    }, function (err) {
      var why = reasonOf(err);
      setState(why === "error" ? "nocamera" : why); report(why);
    });
  }

  function capture() {
    canvas.getContext("2d").drawImage(video, 0, 0, 480, 360);
    canvas.toBlob(function (b) {
      if (!b) { setState("failed", "The picture could not be taken. Try again."); return; }
      blob = b; key = uuid(); attempt = 0;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(b); still.src = objectUrl;
      stopStream(); setState("captured");
    }, "image/jpeg", 0.7);
  }

  function send() {
    setState("sending");
    var form = new FormData();
    form.append("selfie", blob, "selfie.jpg"); form.append("device_id", deviceId());
    post(form, key).then(function (r) { return r.json().then(function (j) { return [r.status, j]; }); }).then(function (res) {
      if (res[0] === 200 && res[1].ok) { window.location.href = res[1].next; return; }
      if (res[0] === 401) { window.location.href = "/login"; return; }
      if (res[0] >= 500 && attempt < 3) { attempt++; setTimeout(send, 600 * attempt * attempt); return; }
      setState("failed", res[1].message ? res[1].message + " You can send it again or retake it." : TEXT.failed);
    }).catch(function () {
      if (attempt < 3) { attempt++; setTimeout(send, 600 * attempt * attempt); return; }
      setState("failed");
    });
  }

  document.getElementById("selfie-capture").addEventListener("click", capture);
  document.getElementById("selfie-confirm").addEventListener("click", send);
  document.getElementById("selfie-retry").addEventListener("click", send);  // the same picture, the same key
  document.getElementById("selfie-retake").addEventListener("click", function () { stopStream(); start(); });
  document.getElementById("selfie-again").addEventListener("click", function () { reported = false; start(); });
  window.addEventListener("pagehide", stopStream);
  start();
})();
