const canvas = document.querySelector(".voice-field");
const ctx = canvas.getContext("2d");
let width = 0;
let height = 0;
let tick = 0;

function resizeCanvas() {
  const ratio = window.devicePixelRatio || 1;
  width = canvas.clientWidth;
  height = canvas.clientHeight;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function drawWave() {
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

  window.requestAnimationFrame(drawWave);
}

resizeCanvas();
drawWave();
window.addEventListener("resize", resizeCanvas);

const demoButton = document.querySelector("#demoButton");
const hudText = document.querySelector("#hudText");
const dictationText = document.querySelector("#dictationText");

const states = [
  {
    hud: "Dictating",
    text: "Testing one two three. Bolo is now faster to launch and feels better on macOS.",
  },
  {
    hud: "Thinking",
    text: "Testing one two three. Bolo is now faster to launch and feels better on macOS.",
  },
  {
    hud: "Inserted",
    text: "Testing one two three. Bolo is now faster to launch and feels better on macOS.",
  },
];

let stateIndex = 0;

demoButton.addEventListener("click", () => {
  const state = states[stateIndex];
  hudText.textContent = state.hud;
  dictationText.textContent = state.text;
  stateIndex = (stateIndex + 1) % states.length;
});
