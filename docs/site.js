/* Bolo site interactions:
   1. Hold-to-talk instrument (the hero demo runs the product's real gesture)
   2. Scroll reveals
   The 3D voice field lives in the inline module in index.html and listens
   for the bolo-hold / bolo-release events dispatched here. */

(function () {
  "use strict";

  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- scroll reveals ---------- */

  var revealEls = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("in"); });
  }

  /* ---------- 2D voice-field fallback ----------
     Runs whenever WebGL is unavailable, the three.js CDN is slow, or the
     3D module fails. Same behavior contract: idle wave, amber surge while
     the key is held, green pulse on release. The 3D module takes over and
     stops this one via window.__bolo3dStop() when it initializes. */

  var webglOK = (function () {
    try {
      var c = document.createElement("canvas");
      return !!(c.getContext("webgl2") || c.getContext("webgl"));
    } catch (err) { return false; }
  })();

  var fieldMode = null;
  var cvs2d = document.getElementById("voiceField2d");
  var ctx2d = cvs2d ? cvs2d.getContext("2d") : null;
  var fieldRAF = null;
  var holdLevel = 0, holdTarget = 0;
  var pulseT2d = -1;
  var pointerX = null;

  function mixColor(a, b, k) {
    return [
      Math.round(a[0] + (b[0] - a[0]) * k),
      Math.round(a[1] + (b[1] - a[1]) * k),
      Math.round(a[2] + (b[2] - a[2]) * k)
    ];
  }
  var C_IDLE = [63, 112, 88];
  var C_AMBER = [240, 165, 63];
  var C_GREEN = [6, 182, 106];

  function size2d() {
    if (!cvs2d) return;
    var r = Math.min(window.devicePixelRatio || 1, 2);
    cvs2d.width = cvs2d.clientWidth * r;
    cvs2d.height = cvs2d.clientHeight * r;
  }

  function draw2d(now) {
    var w = cvs2d.width, h = cvs2d.height;
    ctx2d.clearRect(0, 0, w, h);
    var t = now / 1000;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var barW = Math.max(2, w * 0.0022);
    var step = barW * 4.2;
    // Cap heights in absolute pixels so narrow, tall heroes keep body copy clear.
    var baseH = Math.min(h * 0.16, 90 * dpr);
    var maxH = Math.min(h * 0.52, 300 * dpr);
    for (var x = 0; x < w; x += step) {
      var wave = Math.sin(x * 0.012 + t * 1.25) * 0.5 + Math.sin(x * 0.004 - t * 0.65) * 0.5;
      var hgt = baseH + (wave * 0.5 + 0.5) * baseH * 1.5;
      if (pointerX !== null) {
        var d = Math.abs(x - pointerX);
        hgt += Math.exp(-(d * d) / (2 * (w * 0.06) * (w * 0.06))) * maxH * 0.55;
      }
      hgt *= 1 + holdLevel * 1.7;
      if (pulseT2d >= 0) {
        var dist = Math.abs(x - w / 2);
        var front = pulseT2d * w * 0.7;
        var band = Math.exp(-((dist - front) * (dist - front)) / (2 * (w * 0.07) * (w * 0.07)));
        hgt += band * maxH * Math.max(0, 1 - pulseT2d);
      }
      hgt = Math.min(hgt, maxH + baseH);
      var col = C_IDLE;
      if (holdLevel > 0.02) col = mixColor(C_IDLE, C_AMBER, Math.min(1, holdLevel * (0.4 + (hgt / maxH) * 0.8)));
      if (pulseT2d >= 0) col = mixColor(col, C_GREEN, Math.max(0, 1 - pulseT2d) * 0.8);
      ctx2d.fillStyle = "rgba(" + col[0] + "," + col[1] + "," + col[2] + ",0.85)";
      ctx2d.fillRect(x, h - hgt, barW, hgt);
    }
  }

  function drawStaticBars() {
    var w = cvs2d.width, h = cvs2d.height;
    ctx2d.clearRect(0, 0, w, h);
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var barW = Math.max(2, w * 0.0022);
    var step = barW * 4.2;
    var maxBar = Math.min(h * 0.2, 220 * dpr);
    for (var x = 0; x < w; x += step) {
      var wave = Math.sin(x * 0.012) * 0.5 + Math.sin(x * 0.004) * 0.5;
      var hgt = h * 0.06 + (wave * 0.5 + 0.5) * maxBar * 0.6;
      ctx2d.fillStyle = "rgba(63,112,88,0.5)";
      ctx2d.fillRect(x, h - hgt, barW, hgt);
    }
  }

  function start2dField() {
    if (fieldMode || !ctx2d) return;
    fieldMode = "2d";
    cvs2d.style.display = "block";
    size2d();
    var staticMode = false;
    window.addEventListener("resize", function () {
      size2d();
      if (staticMode) drawStaticBars();
    });

    if (reduced || window.matchMedia("(max-width: 760px)").matches) {
      staticMode = true;
      drawStaticBars();
      return;
    }

    window.addEventListener("pointermove", function (e) {
      var rect = cvs2d.getBoundingClientRect();
      pointerX = (e.clientX - rect.left) * (cvs2d.width / rect.width);
    }, { passive: true });
    window.addEventListener("bolo-hold", function () { holdTarget = 1; });
    window.addEventListener("bolo-release", function () { holdTarget = 0; pulseT2d = 0; });

    var last = performance.now();
    var loop = function (now) {
      fieldRAF = requestAnimationFrame(loop);
      var dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      if (document.hidden) return;
      var rect = cvs2d.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) return;
      holdLevel += (holdTarget - holdLevel) * Math.min(1, dt * 5);
      if (pulseT2d >= 0) { pulseT2d += dt * 2.2; if (pulseT2d > 1.3) pulseT2d = -1; }
      draw2d(now);
    };
    fieldRAF = requestAnimationFrame(loop);
  }

  window.__bolo3dStop = function () {
    if (fieldRAF) cancelAnimationFrame(fieldRAF);
    fieldRAF = null;
    if (cvs2d) cvs2d.style.display = "none";
    fieldMode = "3d";
  };

  if (webglOK) {
    var fallbackArmed = false;
    document.addEventListener("bolo-3d-failed", function () {
      if (!fallbackArmed) { fallbackArmed = true; start2dField(); }
    });
    // If the CDN is slow or the module never reports success, take over with 2D.
    setTimeout(function () {
      if (!fallbackArmed && !window.__bolo3d) { fallbackArmed = true; start2dField(); }
    }, 2000);
  } else {
    start2dField();
  }

  /* ---------- the instrument ---------- */

  var key = document.getElementById("holdKey");
  var chip = document.getElementById("statusChip");
  var statusText = document.getElementById("statusText");
  var line = document.getElementById("transcriptLine");
  if (!key || !chip || !statusText || !line) return;

  // The demo sentence: filler words and a mid-sentence self-correction,
  // exactly the artifacts Bolo's cleanup removes. KEPT survives; AWAY and
  // fillers strike out and fade on release.
  var TOKENS = [
    { t: "um ", kind: "away" },
    { t: "let\u2019s move the sync to ", kind: "keep" },
    { t: "September 15th", kind: "away" },
    { t: " wait, no, ", kind: "away" },
    { t: "actually, October 15th", kind: "keep" },
    { t: ", um, ", kind: "away" },
    { t: "thanks.", kind: "away" }
  ];
  var CLEAN_HTML =
    "Let\u2019s move the sync to <span class=\"clean-mark\">October 15th</span>.";
  var PLACEHOLDER = '<span class="transcript-placeholder">Your sentence shows up here, cleaned up.</span>';

  var holding = false;
  var typeTimer = null;
  var resetTimer = null;
  var tokenIndex = 0;
  var startedAt = 0;

  function setState(state, label) {
    chip.dataset.state = state;
    line.dataset.state = state;
    statusText.textContent = label;
    key.setAttribute("aria-pressed", state === "listening" ? "true" : "false");
  }

  function spanFor(token) {
    var span = document.createElement("span");
    span.textContent = token.t;
    if (token.kind === "away") span.className = "t-fill";
    else span.className = "transcript-raw";
    return span;
  }

  function startHold() {
    if (holding) return;
    holding = true;
    window.__boloHolding = true;
    startedAt = performance.now();
    clearTimeout(resetTimer);
    clearInterval(typeTimer);

    if (reduced) {
      // No animation: jump straight to the end state on any press.
      line.innerHTML = '<span class="typed-wrap">' + CLEAN_HTML + "</span>";
      setState("landed", "Landed");
      window.dispatchEvent(new CustomEvent("bolo-release"));
      return;
    }

    line.querySelector(".transcript-placeholder")?.remove();
    line.innerHTML = "";
    var wrap = document.createElement("span");
    wrap.className = "typed-wrap";
    line.appendChild(wrap);
    tokenIndex = 0;
    setState("listening", "Listening");
    key.classList.add("is-holding");
    window.dispatchEvent(new CustomEvent("bolo-hold", { detail: true }));

    var caret = document.createElement("span");
    caret.className = "t-caret";
    caret.setAttribute("aria-hidden", "true");
    wrap.appendChild(caret);

    typeTimer = setInterval(function () {
      if (tokenIndex >= TOKENS.length) {
        clearInterval(typeTimer);
        typeTimer = null;
        return;
      }
      var token = TOKENS[tokenIndex++];
      wrap.insertBefore(spanFor(token), caret);
    }, 135);
  }

  function endHold() {
    if (!holding) return;
    holding = false;
    window.__boloHolding = false;
    key.classList.remove("is-holding");
    window.dispatchEvent(new CustomEvent("bolo-release"));
    clearInterval(typeTimer);
    typeTimer = null;

    var finish = function () {
      line.innerHTML = '<span class="typed-wrap">' + CLEAN_HTML + "</span>";
      setState("landed", "Landed");
      // In ?demo=land QA mode, keep the landed state on screen.
      if (demoParam === "land") return;
      resetTimer = setTimeout(function () {
        line.innerHTML = PLACEHOLDER;
        line.removeAttribute("data-state");
        chip.dataset.state = "ready";
        statusText.textContent = "Ready";
      }, 5200);
    };

    if (reduced) return;

    // Strike out fillers and the self-corrected phrase, then land the clean text.
    line.querySelectorAll(".t-fill").forEach(function (el) {
      el.classList.add("t-gone");
    });
    var caret = line.querySelector(".t-caret");
    if (caret) caret.remove();
    setState("landed", "Landed");
    setTimeout(finish, 620);
  }

  key.addEventListener("pointerdown", function (e) {
    e.preventDefault();
    startHold();
  });
  window.addEventListener("pointerup", endHold);
  window.addEventListener("pointercancel", endHold);
  key.addEventListener("contextmenu", function (e) { e.preventDefault(); });

  key.addEventListener("keydown", function (e) {
    if (e.repeat) return;
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      startHold();
    }
  });
  key.addEventListener("keyup", function (e) {
    if (e.key === " " || e.key === "Enter") endHold();
  });
  key.addEventListener("blur", endHold);

  // Debug/demo affordance for visual QA: ?demo=hold keeps the key pressed,
  // ?demo=land shows the full press-release cycle automatically.
  var demoParam = new URLSearchParams(window.location.search).get("demo");
  if (demoParam === "hold") startHold();
  else if (demoParam === "land") { startHold(); setTimeout(endHold, 520); }
})();
