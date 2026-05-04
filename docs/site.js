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

// Demo button cycling
const demoButton = document.querySelector("#demoButton");
const hudText = document.querySelector("#hudText");
const dictationText = document.querySelector("#dictationText");

const SPOKEN = "Send that email to the team about Thursday's sprint review.";

const states = [
  {
    hud: "Dictating",
    text: SPOKEN,
  },
  {
    hud: "Thinking",
    text: SPOKEN,
  },
  {
    hud: "Inserted",
    text: SPOKEN,
  },
];

let stateIndex = 0;

demoButton.addEventListener("click", () => {
  const state = states[stateIndex];
  hudText.textContent = state.hud;
  dictationText.textContent = state.text;
  stateIndex = (stateIndex + 1) % states.length;
  demoButton.textContent = stateIndex === 0 ? "Try again" : "Next step";
});

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
