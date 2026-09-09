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
      hgt *= 1 + holdLevel * 1.7 + (window.__boloVoiceLevel || 0) * 1.8;
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

  /* ---------- live demo: real microphone + browser speech recognition ----------
     The scripted sentence is the FALLBACK, shown only when the mic is
     unavailable (denied, missing, unsupported) or a QA hook forces it.
     When live, holding the key transcribes what the speaker actually says
     via the browser's SpeechRecognition API; release applies a simplified
     local cleanup. The 3D/2D fields read window.__boloVoiceLevel for real
     amplitude, so the visual responds to the actual voice in the room. */

  var demoParam = new URLSearchParams(window.location.search).get("demo");
  var forceScripted = demoParam === "hold" || demoParam === "land";
  var liveMode = false;

  var SRClass = window.SpeechRecognition || window.webkitSpeechRecognition || null;

  var mic = {
    state: "unknown", // unknown | pending | ready | denied
    ctx: null,
    analyser: null,
    buf: null,
    raf: null,
    stream: null,
    rec: null,
    recActive: false,
    recFinal: "",
    recInterim: "",
    recFailedNotified: false
  };

  function micSupported() {
    return !!SRClass && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function requestMic() {
    mic.state = "pending";
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      mic.state = "ready";
      mic.stream = stream;
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!mic.ctx && Ctx) mic.ctx = new Ctx();
      if (mic.ctx && mic.ctx.state === "suspended") mic.ctx.resume();
      if (mic.ctx) {
        var source = mic.ctx.createMediaStreamSource(stream);
        mic.analyser = mic.ctx.createAnalyser();
        mic.analyser.fftSize = 512;
        source.connect(mic.analyser);
        mic.buf = new Uint8Array(mic.analyser.fftSize);
      }
      // Tracks stay open only while the key is held; if permission resolves
      // after release, wait for the next hold instead of listening idle.
      if (holding) {
        startVoiceRaf();
        startRecognition();
      }
    }).catch(function () {
      mic.state = "denied";
      if (holding) {
        // Permission refused mid-hold: fall back to the scripted sentence so
        // the demo never stalls on an empty transcript.
        liveMode = false;
        startScriptedTyping();
        showDemoBadge();
      }
    });
  }

  function startVoiceRaf() {
    if (!mic.analyser) return;
    var tick = function () {
      mic.raf = requestAnimationFrame(tick);
      if (document.hidden || !mic.analyser) return;
      mic.analyser.getByteTimeDomainData(mic.buf);
      var sum = 0;
      for (var i = 0; i < mic.buf.length; i++) {
        var v = (mic.buf[i] - 128) / 128;
        sum += v * v;
      }
      var rms = Math.sqrt(sum / mic.buf.length);
      window.__boloVoiceLevel = Math.min(1, rms * 4.5);
    };
    tick();
  }

  function stopVoiceRaf() {
    if (mic.raf) cancelAnimationFrame(mic.raf);
    mic.raf = null;
    window.__boloVoiceLevel = 0;
  }

  function startRecognition() {
    if (!SRClass) return;
    mic.rec = new SRClass();
    mic.rec.lang = navigator.language || "en-US";
    mic.rec.continuous = true;
    mic.rec.interimResults = true;
    mic.rec.onresult = function (event) {
      var interim = "", finals = "";
      for (var i = event.resultIndex; i < event.results.length; i++) {
        var result = event.results[i];
        if (result.isFinal) finals += result[0].transcript;
        else interim += result[0].transcript;
      }
      if (finals) mic.recFinal = (mic.recFinal + " " + finals).trim();
      mic.recInterim = interim;
      if (holding) updateLiveTranscript();
    };
    mic.rec.onerror = function (event) {
      var kind = event && event.error;
      if (kind === "not-allowed" || kind === "service-not-allowed" || kind === "audio-capture") {
        mic.recFailedNotified = true;
        showDemoBadge();
      }
      // no-speech / network / aborted: the onend restart loop keeps listening.
    };
    mic.rec.onend = function () {
      mic.recActive = false;
      if (holding && mic.stream && !mic.recFailedNotified) {
        setTimeout(function () {
          if (holding && mic.stream) {
            try { mic.rec.start(); mic.recActive = true; } catch (err) { /* ignore */ }
          }
        }, 250);
      }
    };
    try { mic.rec.start(); mic.recActive = true; } catch (err) { /* already started */ }
  }

  function stopRecognition() {
    if (mic.rec && mic.recActive) {
      try { mic.rec.stop(); } catch (err) { /* ignore */ }
      mic.recActive = false;
    }
  }

  function releaseMic() {
    stopVoiceRaf();
    stopRecognition();
    if (mic.stream) {
      mic.stream.getTracks().forEach(function (track) { track.stop(); });
      mic.stream = null;
    }
  }

  function clearTranscript() {
    line.innerHTML = "";
    var wrap = document.createElement("span");
    wrap.className = "typed-wrap";
    line.appendChild(wrap);
    var caret = document.createElement("span");
    caret.className = "t-caret";
    caret.setAttribute("aria-hidden", "true");
    wrap.appendChild(caret);
  }

  function updateLiveTranscript() {
    if (!holding || !liveMode) return;
    var wrap = line.querySelector(".typed-wrap");
    if (!wrap) return;
    var spoken = (mic.recFinal + " " + mic.recInterim).replace(/\s+/g, " ").trim();
    var caret = wrap.querySelector(".t-caret");
    wrap.innerHTML = "";
    if (mic.recFinal) {
      var finalSpan = document.createElement("span");
      finalSpan.className = "transcript-raw";
      finalSpan.textContent = mic.recFinal;
      wrap.appendChild(finalSpan);
    }
    if (mic.recInterim) {
      var interimSpan = document.createElement("span");
      interimSpan.className = "t-interim";
      interimSpan.textContent = (mic.recFinal ? " " : "") + mic.recInterim;
      wrap.appendChild(interimSpan);
    }
    if (!spoken) {
      var hintSpan = document.createElement("span");
      hintSpan.className = "t-interim";
      hintSpan.textContent = "speak while holding the key";
      wrap.appendChild(hintSpan);
    }
    if (caret) wrap.appendChild(caret);
  }

  function showDemoBadge() {
    if (line.querySelector(".demo-badge")) return;
    var badge = document.createElement("span");
    badge.className = "demo-badge";
    badge.textContent = "demo sentence \u00b7 mic unavailable";
    line.appendChild(badge);
  }

  var FILLER_RE = /\b(uh+|um+|er+|erm+|mmm+)\b[,.!?]*\s*/gi;
  function cleanLiveText(text) {
    var t = (text || "").replace(/\s+/g, " ").trim();
    if (!t) return "";
    t = t.replace(FILLER_RE, " ").replace(/\s{2,}/g, " ").trim();
    if (!t) return "";
    t = t.charAt(0).toUpperCase() + t.slice(1);
    if (!/[.!?]$/.test(t)) t += ".";
    return t;
  }

  function scheduleReset() {
    // In ?demo=land QA mode, keep the landed state on screen.
    if (demoParam === "land") return;
    resetTimer = setTimeout(function () {
      line.innerHTML = PLACEHOLDER;
      line.removeAttribute("data-state");
      chip.dataset.state = "ready";
      statusText.textContent = "Ready";
    }, 5200);
  }

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
    mic.recFinal = "";
    mic.recInterim = "";

    if (reduced) {
      // No animation: jump straight to the end state on any press.
      line.innerHTML = '<span class="typed-wrap">' + CLEAN_HTML + "</span>";
      setState("landed", "Landed");
      window.dispatchEvent(new CustomEvent("bolo-release"));
      return;
    }

    line.querySelector(".transcript-placeholder")?.remove();

    // Live mode when the mic is available (or permission is in flight);
    // scripted fallback when unavailable or forced by a QA hook.
    liveMode = false;
    if (!forceScripted && micSupported()) {
      if (mic.state === "ready" || mic.state === "pending") {
        liveMode = true;
      } else if (mic.state === "unknown") {
        mic.state = "pending";
        requestMic();
        liveMode = true; // optimistic: show the live caret while permission resolves
      }
    }

    setState("listening", "Listening");
    key.classList.add("is-holding");
    window.dispatchEvent(new CustomEvent("bolo-hold", { detail: true }));

    if (liveMode) {
      clearTranscript();
      updateLiveTranscript(); // show the "speak while holding" hint before the first result
      if (mic.state === "ready" && !mic.recActive) startRecognition();
      return;
    }

    startScriptedTyping();
    if (mic.state === "denied") showDemoBadge();
  }

  function startScriptedTyping() {
    line.innerHTML = "";
    var wrap = document.createElement("span");
    wrap.className = "typed-wrap";
    line.appendChild(wrap);
    tokenIndex = 0;
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
    releaseMic(); // tracks + analyser die with the hold; Chrome flushes finals after stop()

    if (liveMode && mic.state === "ready") {
      // Give the final recognition flush a beat or two before landing.
      var attempts = 0;
      var land = function () {
        var spoken = (mic.recFinal + " " + mic.recInterim).replace(/\s+/g, " ").trim();
        if (!spoken && attempts < 2) {
          attempts++;
          setTimeout(land, 220);
          return;
        }
        var cleaned = cleanLiveText(spoken);
        setState("landed", "Landed");
        if (cleaned) {
          line.innerHTML = '<span class="typed-wrap">' + escapeHtml(cleaned) + "</span>";
        } else {
          line.innerHTML = '<span class="typed-wrap"><span class="t-none">Nothing recognized. Hold the key and speak.</span></span>';
        }
        scheduleReset();
      };
      land();
      return;
    }

    // Live hold that never got mic permission: degrade to the scripted landing
    // so the visitor still sees the full flow.
    if (liveMode && mic.state !== "ready") {
      liveMode = false;
      startScriptedTyping();
      showDemoBadge();
    }

    var finish = function () {
      line.innerHTML = '<span class="typed-wrap">' + CLEAN_HTML + "</span>";
      setState("landed", "Landed");
      scheduleReset();
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

  /* ---------- real keyboard: the page does what the keycap says ----------
     Hold SPACE or the RIGHT OPTION/ALT key anywhere while the hero is on
     screen to run the demo. Space stays a scroll key once the hero is
     scrolled past, so interception is scoped to hero visibility. */
  var heroInView = true;
  var heroEl = document.querySelector(".hero");
  if (heroEl && "IntersectionObserver" in window) {
    heroInView = false; // set on first observation tick
    new IntersectionObserver(function (entries) {
      heroInView = entries[0].isIntersecting;
    }, { threshold: 0.15 }).observe(heroEl);
  }

  window.addEventListener("keydown", function (e) {
    if (!heroInView) return;
    var isOption = e.code === "AltRight" || e.code === "AltLeft";
    if (e.code !== "Space" && !isOption) return;
    // Do not hijack real shortcuts; altKey is inherent to the Option key
    // itself, so it is allowed through while metaKey/ctrlKey are not.
    if (e.metaKey || e.ctrlKey) return;
    var tag = e.target && e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    e.preventDefault(); // suppress the space-scroll on every repeat, not just the first
    if (e.repeat) return;
    startHold();
  });
  window.addEventListener("keyup", function (e) {
    if (e.code !== "Space" && e.code !== "AltRight" && e.code !== "AltLeft") return;
    endHold();
  });

  // Debug/demo affordance for visual QA: ?demo=hold keeps the key pressed,
  // ?demo=land shows the full press-release cycle automatically. Both force
  // the scripted demo so captures stay deterministic.
  if (demoParam === "hold") startHold();
  else if (demoParam === "land") { startHold(); setTimeout(endHold, 520); }
})();
