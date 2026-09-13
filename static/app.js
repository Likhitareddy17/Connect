const breathStart = document.querySelector("#breath-start");
const breathStop = document.querySelector("#breath-stop");
const breathCircle = document.querySelector("#breath-circle");
const breathWord = document.querySelector("#breath-word");
const breathPhase = document.querySelector("#breath-phase");
const breathStatus = document.querySelector("#breath-status");

let timer = null;
let index = 0;
const steps = [
  { word: "Inhale", phase: "receive", expand: true, ms: 4000 },
  { word: "Exhale", phase: "release", expand: false, ms: 6000 },
];

function stopBreath(label = "ready") {
  clearTimeout(timer);
  timer = null;
  index = 0;
  if (breathWord) breathWord.textContent = label === "complete" ? "Complete" : "Begin";
  if (breathPhase) breathPhase.textContent = label;
  if (breathStatus) {
    breathStatus.textContent = label === "complete" ? "Well done. Carry this softness forward." : "";
  }
  if (breathCircle) breathCircle.classList.remove("expand");
}

function runBreath() {
  if (breathStatus) breathStatus.textContent = "";
  const step = steps[index % steps.length];
  breathWord.textContent = step.word;
  breathPhase.textContent = step.phase;
  breathCircle.classList.toggle("expand", step.expand);
  index += 1;
  if (index >= 8) {
    stopBreath("complete");
    return;
  }
  timer = setTimeout(runBreath, step.ms);
}

if (breathStart && breathStop) {
  breathStart.addEventListener("click", () => {
    stopBreath();
    runBreath();
  });
  breathStop.addEventListener("click", () => stopBreath());
}

const mantraCard = document.querySelector(".mantra-card");
const jaapCount = document.querySelector("#jaap-count");
const jaapPlus = document.querySelector("#jaap-plus");
const jaapMinus = document.querySelector("#jaap-minus");
const jaapReset = document.querySelector("#jaap-reset");
const mantraAudio = document.querySelector("#mantra-audio");
const jaapStatus = document.querySelector("#jaap-status");
let jaapSaveTimer = null;

function saveJaapCount(count) {
  if (!mantraCard) return;
  clearTimeout(jaapSaveTimer);
  if (jaapStatus) {
    jaapStatus.classList.remove("error");
    jaapStatus.textContent = "Saving count...";
  }
  jaapSaveTimer = setTimeout(() => {
    fetch(mantraCard.dataset.saveUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count }),
    })
      .then((response) => {
        if (!response.ok) throw new Error("Save failed");
        return response.json();
      })
      .then(() => {
        if (jaapStatus) jaapStatus.textContent = "Saved.";
      })
      .catch(() => {
        if (jaapStatus) {
          jaapStatus.classList.add("error");
          jaapStatus.textContent = "Count not saved. Please try again.";
        }
      });
  }, 400);
}

function setJaapCount(nextCount) {
  const count = Math.max(0, nextCount);
  jaapCount.textContent = count;
  saveJaapCount(count);
}

if (mantraCard && jaapCount) {
  jaapCount.textContent = mantraCard.dataset.initialCount || "0";

  jaapPlus.addEventListener("click", () => {
    setJaapCount(Number.parseInt(jaapCount.textContent, 10) + 1);
  });

  jaapMinus.addEventListener("click", () => {
    setJaapCount(Number.parseInt(jaapCount.textContent, 10) - 1);
  });

  jaapReset.addEventListener("click", () => setJaapCount(0));

  mantraAudio.addEventListener("click", () => {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const chant = new SpeechSynthesisUtterance(mantraCard.dataset.mantra);
    chant.rate = 0.72;
    chant.pitch = 0.92;
    chant.volume = 1;
    window.speechSynthesis.speak(chant);
  });
}

document.querySelectorAll(".mood-pill input").forEach((input) => {
  input.addEventListener("change", () => {
    document.querySelectorAll(".mood-pill").forEach((pill) => {
      pill.classList.toggle("active", pill.contains(input));
    });
  });
});
