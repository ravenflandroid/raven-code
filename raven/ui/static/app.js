const form = document.querySelector("#runForm");
const runChip = document.querySelector("#runChip");
const startButton = document.querySelector("#startButton");
const stopButton = document.querySelector("#stopButton");
const terminal = document.querySelector("#terminal");
const artifacts = document.querySelector("#artifacts");
const terminalForm = document.querySelector("#terminalForm");
const terminalInput = document.querySelector("#terminalInput");

let currentRunId = "";
let eventSource = null;

function setRunning(active) {
  startButton.disabled = active;
  stopButton.disabled = !active;
}

const agentTitles = {
  intake: "Issue Intake",
  repo: "Repository Checkout",
  media: "Media Frames",
  emulator: "Emulator Setup",
  agent1: "Action Sequence",
  agent2: "Emulator Replay",
  fallback: "Static Fallback",
  agent3: "HDG Generation",
  agent4: "Localization",
  done: "Complete",
  system: "System",
  terminal: "Terminal",
};

function appendTerminal(line) {
  terminal.textContent += `${line}\n`;
  terminal.scrollTop = terminal.scrollHeight;
}

function updateAgent(event) {
  const mapped = ["repo", "media", "emulator"].includes(event.agent) ? "intake" : event.agent;
  const card = document.querySelector(`[data-agent="${mapped}"]`);
  if (!card) return;
  card.classList.remove("running", "complete", "warning", "failed", "cancelled");
  card.classList.add(event.status);
  const title = agentTitles[event.agent] || event.agent;
  card.querySelector("h3").textContent = title;
  card.querySelector("p").textContent = event.message;
}

async function startRun(payload) {
  const response = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setRunning(true);
  terminal.textContent = "";
  artifacts.innerHTML = "<p>Run in progress.</p>";
  document.querySelectorAll(".agent").forEach((card) => {
    card.classList.remove("running", "complete", "warning", "failed", "cancelled");
    card.querySelector("p").textContent = "Waiting for live update.";
  });

  const payload = Object.fromEntries(new FormData(form).entries());
  try {
    const { run_id } = await startRun(payload);
    currentRunId = run_id;
    runChip.textContent = `Run ${run_id}`;
    appendTerminal(`Connected to run ${run_id}`);
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/events?run_id=${run_id}`);
    eventSource.onmessage = (message) => {
      const data = JSON.parse(message.data);
      appendTerminal(`[${data.ts}] ${data.agent.toUpperCase()} ${data.status}: ${data.message}`);
      updateAgent(data);
      if (data.agent === "done" && data.status === "complete") {
        setRunning(false);
        artifacts.innerHTML = `<div class="artifact-path">${data.message}</div>`;
      }
      if (data.status === "failed" || data.status === "cancelled") {
        setRunning(false);
      }
    };
    eventSource.onerror = () => {
      setRunning(false);
    };
  } catch (error) {
    appendTerminal(`Failed to start run: ${error.message}`);
    setRunning(false);
  }
});

stopButton.addEventListener("click", async () => {
  if (!currentRunId) return;
  stopButton.disabled = true;
  appendTerminal(`Requesting stop for run ${currentRunId}…`);
  try {
    await fetch(`/api/run/${currentRunId}/stop`, { method: "POST" });
  } catch (error) {
    appendTerminal(`Stop request failed: ${error.message}`);
  }
});

terminalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentRunId) {
    appendTerminal("Start a run before sending terminal commands.");
    return;
  }
  const command = terminalInput.value.trim();
  if (!command) return;
  terminalInput.value = "";
  await fetch("/api/terminal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: currentRunId, command }),
  });
});

