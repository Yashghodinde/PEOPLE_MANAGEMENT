/* Selfie step of login (M06.1). Camera -> canvas 480x360 -> JPEG q0.7 -> POST with one Idempotency-Key,
   retried 3 times with backoff (same key, so the server makes exactly one session). */
(function () {
  "use strict";
  var root = document.getElementById("selfie-root");
  if (!root) return;
  var video = document.getElementById("selfie-video"), canvas = document.getElementById("selfie-canvas");
  var btn = document.getElementById("selfie-capture"), msg = document.getElementById("selfie-msg");
  var url = root.getAttribute("data-url"), csrf = root.getAttribute("data-csrf");
  function say(t) { msg.textContent = t; }
  function uuid() { return (window.otsUuid7 ? window.otsUuid7() : crypto.randomUUID()); }
  function deviceId() {
    try { var d = localStorage.getItem("ots-device"); if (!d) { d = uuid(); localStorage.setItem("ots-device", d); } return d; }
    catch (e) { return ""; }
  }
  function post(form, key) {
    return fetch(url, { method: "POST", body: form, credentials: "same-origin",
      headers: { "Idempotency-Key": key, "X-CSRFToken": csrf } });
  }
  function unavailable() {
    var f = new FormData(); f.append("camera", "unavailable");
    post(f, uuid()).then(function (r) { return r.json(); }).then(function (j) { say(j.message || "Camera required for attendance. Contact the administrator."); })
      .catch(function () { say("Camera required for attendance. Contact the administrator."); });
    btn.disabled = true;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { unavailable(); return; }
  navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } })
    .then(function (stream) { video.srcObject = stream; btn.disabled = false; })
    .catch(unavailable);

  var key = uuid(); // one key for this attempt: a repeated submission cannot make a second session
  btn.addEventListener("click", function () {
    btn.disabled = true; say("");
    canvas.getContext("2d").drawImage(video, 0, 0, 480, 360);
    canvas.toBlob(function (blob) {
      var form = new FormData();
      form.append("selfie", blob, "selfie.jpg"); form.append("device_id", deviceId());
      var attempt = 0;
      (function send() {
        post(form, key).then(function (r) { return r.json().then(function (j) { return [r.status, j]; }); })
          .then(function (res) {
            if (res[0] === 200 && res[1].ok) { window.location.href = res[1].next; return; }
            if (res[0] >= 500 && attempt < 3) { attempt++; setTimeout(send, 600 * attempt * attempt); return; }
            say(res[1].message || "Could not log you in. Try again."); btn.disabled = res[0] === 401;
            if (res[0] === 401) window.location.href = "/login";
          })
          .catch(function () {
            if (attempt < 3) { attempt++; setTimeout(send, 600 * attempt * attempt); return; }
            say("The picture could not be sent. Check the connection and try again."); btn.disabled = false;
          });
      })();
    }, "image/jpeg", 0.7);
  });
})();
