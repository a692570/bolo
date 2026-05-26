const canvas = document.querySelector(".voice-field");
const ctx = canvas.getContext("2d");
let width = 0;
let height = 0;
let tick = 0;
let animating = true;
let rafId = null;

function resizeCanvas() {
  const ratio = window.devicePixelRatio || 1;
  width = canvas.clientWidth;
  height = canvas.clientHeight;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function drawWave() {
  if (!animating) return;
  tick += 0.014;
  ctx.clearRect(0, 0, width, height);

  const center = height * 0.5;
  const rows = 4;
  for (let row = 0; row < rows; row += 1) {
    ctx.beginPath();
    const offset = row * 38;
    const alpha = 0.18 - row * 0.03;
    ctx.strokeStyle = `rgba(6, 182, 106, ${alpha})`;
    ctx.lineWidth = 2;

    for (let x = -20; x <= width + 20; x += 10) {
      const y =
        center +
        Math.sin(x * 0.014 + tick * (2.4 + row)) * (42 - row * 5) +
        Math.sin(x * 0.006 + tick * 2 + row) * 28 +
        offset -
        58;
      if (x === -20) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
  }

  rafId = window.requestAnimationFrame(drawWave);
}

resizeCanvas();
drawWave();
window.addEventListener("resize", resizeCanvas);

// Pause RAF when canvas scrolls off screen
const canvasObserver = new IntersectionObserver(
  (entries) => {
    const visible = entries[0].isIntersecting;
    if (visible && !animating) {
      animating = true;
      drawWave();
    } else if (!visible && animating) {
      animating = false;
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
    }
  },
  { threshold: 0 }
);
canvasObserver.observe(canvas);

// Auto-playing hero demo
const demoButton = document.querySelector("#demoButton");
const hudText = document.querySelector("#hudText");
const dictationText = document.querySelector("#dictationText");
const liveDot = document.querySelector(".live-dot");
const hudEl = document.querySelector(".hud");
const hudBars = document.querySelectorAll(".hud-bars i");
const cursorEl = document.querySelector(".cursor");
const editorSurface = document.querySelector(".editor-surface");

const WORDS = ["Send", "that", "email", "to", "the", "team", "about", "Thursday's", "sprint", "review."];

let demoTimers = [];
let wordInterval = null;

function clearDemo() {
  demoTimers.forEach((id) => clearTimeout(id));
  demoTimers = [];
  if (wordInterval !== null) {
    clearInterval(wordInterval);
    wordInterval = null;
  }
}

function schedule(fn, delay) {
  const id = setTimeout(fn, delay);
  demoTimers.push(id);
}

function resetToIdle() {
  // Clear text
  dictationText.textContent = "";
  // HUD back to dictating
  hudText.textContent = "Dictating";
  hudEl.classList.remove("hud--processing");
  // Dot green
  liveDot.style.background = "";
  liveDot.style.boxShadow = "";
  // Bars resume
  hudBars.forEach((bar) => {
    bar.style.animationPlayState = "";
  });
  // Cursor resumes blinking, blue
  cursorEl.style.animationPlayState = "";
  cursorEl.style.background = "";
}

function runDemo() {
  clearDemo();
  resetToIdle();

  // --- STATE 1: DICTATING (0ms) ---
  // Words appear one by one every 280ms
  let wordIndex = 0;
  wordInterval = setInterval(() => {
    if (wordIndex < WORDS.length) {
      dictationText.textContent = WORDS.slice(0, wordIndex + 1).join(" ");
      wordIndex += 1;
    } else {
      clearInterval(wordInterval);
      wordInterval = null;
    }
  }, 280);

  // --- STATE 2: TRANSCRIBING (3500ms) ---
  schedule(() => {
    // Stop word interval if somehow still running
    if (wordInterval !== null) {
      clearInterval(wordInterval);
      wordInterval = null;
    }
    // Ensure all words visible
    dictationText.textContent = WORDS.join(" ");
    // HUD label
    hudText.textContent = "Transcribing";
    // HUD background shift
    hudEl.classList.add("hud--processing");
    // Dot amber
    liveDot.style.background = "#f4b23b";
    liveDot.style.boxShadow = "0 0 0 7px rgba(244,178,59,0.16)";
    // Bars pause (handled by .hud--processing CSS, but also set inline for safety)
    hudBars.forEach((bar) => {
      bar.style.animationPlayState = "paused";
    });
    // Cursor stops blinking
    cursorEl.style.animationPlayState = "paused";
  }, 3500);

  // --- STATE 3: COPIED (3500 + 1800 = 5300ms) ---
  schedule(() => {
    hudText.textContent = "Copied";
    hudEl.classList.remove("hud--processing");
    // Dot green
    liveDot.style.background = "";
    liveDot.style.boxShadow = "";
    // Bars resume
    hudBars.forEach((bar) => {
      bar.style.animationPlayState = "";
    });
    // Editor flash
    editorSurface.classList.add("editor-surface--flash");
    schedule(() => {
      editorSurface.classList.remove("editor-surface--flash");
    }, 400);
    // Cursor flash green then reset to blue
    cursorEl.style.animationPlayState = "";
    cursorEl.style.background = "var(--green)";
    schedule(() => {
      cursorEl.style.background = "";
    }, 300);
  }, 5300);

  // --- RESET PAUSE then loop (5300 + 1500 + 1200 = 8000ms) ---
  schedule(() => {
    runDemo();
  }, 8000);
}

demoButton.textContent = "Replay";
demoButton.addEventListener("click", () => {
  runDemo();
});

// Auto-start after page settles
setTimeout(runDemo, 1000);

// Flow diagram — staggered reveal + active step pulse loop
(function () {
  const diagram = document.querySelector('.flow-diagram');
  if (!diagram) return;

  const items = Array.from(diagram.children); // nodes + arrows in DOM order
  const nodes = Array.from(diagram.querySelectorAll('.flow-node'));

  let revealed = false;
  const observer = new IntersectionObserver(function (entries) {
    if (revealed || !entries[0].isIntersecting) return;
    revealed = true;
    observer.disconnect();

    items.forEach(function (el, i) {
      setTimeout(function () {
        el.style.opacity = '1';
        el.style.transform = 'translateY(0)';
      }, i * 120);
    });

    // Start pulse after all items have entered + small buffer
    var totalDelay = items.length * 120 + 400;
    setTimeout(startPulse, totalDelay);
  }, { threshold: 0.3 });

  observer.observe(diagram);

  var pulseIndex = 0;
  var pulseTimer = null;

  function startPulse() {
    if (pulseTimer !== null) clearTimeout(pulseTimer);
    nodes.forEach(function (n) { n.classList.remove('flow-active'); });
    nodes[pulseIndex].classList.add('flow-active');
    pulseIndex = (pulseIndex + 1) % nodes.length;
    // After the last node, pause 1s longer before looping
    var delay = pulseIndex === 0 ? 2800 : 1800;
    pulseTimer = setTimeout(startPulse, delay);
  }
})();

// Copy button for install section
const terminal = document.querySelector(".terminal");
if (terminal) {
  const pre = terminal.querySelector("pre");
  const copyBtn = document.createElement("button");
  copyBtn.className = "copy-btn";
  copyBtn.type = "button";
  copyBtn.textContent = "Copy";
  terminal.appendChild(copyBtn);

  copyBtn.addEventListener("click", () => {
    const text = pre ? pre.textContent : "";
    navigator.clipboard.writeText(text).then(() => {
      copyBtn.textContent = "Copied!";
      copyBtn.classList.add("copied");
      setTimeout(() => {
        copyBtn.textContent = "Copy";
        copyBtn.classList.remove("copied");
      }, 1500);
    });
  });
}
