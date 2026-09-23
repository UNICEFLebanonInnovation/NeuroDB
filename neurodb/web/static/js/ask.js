// Ask NeuroDB: streams Claude's answer (Server-Sent Events over a POST) into a conversation thread.
import { csrfToken, toast } from "./lib.js";

const root = document.querySelector("[data-ask]");
if (root) init(root);

function newConversationId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
    (c ^ (window.crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16),
  );
}

function init(root) {
  const form = root.querySelector("#ask-form");
  const input = root.querySelector("#ask-input");
  const thread = root.querySelector("#ask-thread");
  const intro = root.querySelector("#ask-intro");
  const submit = root.querySelector("#ask-submit");
  const stop = root.querySelector("#ask-stop");
  const reset = root.querySelector("#ask-new");
  const template = document.querySelector("#ask-turn-template");
  let conversation = newConversationId();
  let controller = null;

  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  root.querySelectorAll("[data-ask-example]").forEach((btn) =>
    btn.addEventListener("click", () => {
      input.value = btn.textContent.trim();
      autosize();
      form.requestSubmit();
    }),
  );
  stop.addEventListener("click", () => controller?.abort());
  reset.addEventListener("click", () => {
    conversation = newConversationId();
    thread.querySelectorAll(".ask-turn").forEach((el) => el.remove());
    intro.hidden = false;
    reset.hidden = true;
    input.focus();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (!question || controller) return;
    input.value = "";
    autosize();
    intro.hidden = true;
    await ask(question);
  });

  async function ask(question) {
    const turn = template.content.firstElementChild.cloneNode(true);
    turn.querySelector(".ask-turn__question").textContent = question;
    thread.append(turn);
    turn.scrollIntoView({ behavior: "smooth", block: "start" });
    const ui = turnUI(turn);

    controller = new AbortController();
    setBusy(true);
    const body = new FormData();
    body.append("question", question);
    body.append("conversation", conversation);
    try {
      const res = await fetch(root.dataset.streamUrl, {
        method: "POST",
        body,
        headers: { "X-CSRFToken": csrfToken(), Accept: "text/event-stream" },
        signal: controller.signal,
      });
      if (res.ok && !(res.headers.get("Content-Type") || "").startsWith("text/event-stream")) {
        // Redirected to the sign-in page: the session expired.
        ui.error("Your session has expired. Reload the page and sign in again.");
        return;
      }
      if (!res.ok || !res.body) {
        let message = `The assistant could not answer (${res.status}).`;
        try {
          message = (await res.json()).error || message;
        } catch {
          /* not JSON */
        }
        ui.error(message);
        return;
      }
      await readEvents(res.body, ui.handle);
      ui.finish();
    } catch (err) {
      if (err.name === "AbortError") ui.error("Stopped.", "muted");
      else ui.error("The connection was interrupted. Please try again.");
    } finally {
      controller = null;
      setBusy(false);
      reset.hidden = false;
      input.focus();
    }
  }

  function setBusy(busy) {
    submit.disabled = busy;
    stop.hidden = !busy;
    root.classList.toggle("is-busy", busy);
  }

  if (root.dataset.initialQuestion) {
    input.value = root.dataset.initialQuestion;
    autosize();
    form.requestSubmit();
  } else {
    input.focus();
  }
}

async function readEvents(stream, handle) {
  const reader = stream.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const data = chunk
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (data) handle(JSON.parse(data));
    }
  }
}

// One question/answer card: live text while streaming, a list of lookups, then the final HTML.
function turnUI(turn) {
  const steps = turn.querySelector(".ask-turn__steps");
  const bodyEl = turn.querySelector(".ask-turn__body");
  const footer = turn.querySelector(".ask-turn__footer");
  const meta = turn.querySelector(".ask-turn__meta");
  let round = 0;
  let live = "";
  let answered = false;
  let lookups = 0;
  let finalText = "";

  const liveBox = () => {
    let el = bodyEl.querySelector(".ask-turn__live");
    if (!el) {
      bodyEl.textContent = "";
      el = document.createElement("div");
      el.className = "ask-turn__live";
      bodyEl.append(el);
    }
    return el;
  };
  const settleSteps = () => steps.querySelectorAll(".is-running").forEach((s) => s.classList.replace("is-running", "is-done"));

  function handle(event) {
    if (event.type === "text") {
      if (event.round !== round) {
        round = event.round;
        live = "";
      }
      live += event.text;
      liveBox().textContent = live;
    } else if (event.type === "tool") {
      // Text written before a lookup is narration ("Let me check…"): move it into the steps list.
      if (live.trim() && event.round === round) {
        const note = document.createElement("li");
        note.className = "ask-step ask-step--note";
        note.textContent = live.trim();
        steps.append(note);
        live = "";
        bodyEl.innerHTML = '<span class="ask-turn__thinking">Looking up data…</span>';
      }
      const step = document.createElement("li");
      step.className = "ask-step is-running";
      step.textContent = event.label;
      steps.append(step);
      lookups += 1;
    } else if (event.type === "done") {
      settleSteps();
      bodyEl.innerHTML = event.html; // sanitized on the server (no scripts, images or styles)
      finalText = event.answer;
      answered = true;
    } else if (event.type === "error") {
      error(event.message);
    }
  }

  function error(message, tone = "danger") {
    settleSteps();
    const box = document.createElement("div");
    box.className = `alert alert-${tone === "muted" ? "secondary" : "danger"} py-2 px-3 mb-0 small`;
    box.textContent = message;
    if (!live.trim()) bodyEl.textContent = "";
    bodyEl.append(box);
  }

  function finish() {
    settleSteps();
    if (!answered) return;
    footer.hidden = false;
    meta.textContent = lookups
      ? `Answered from ${lookups} data lookup${lookups === 1 ? "" : "s"}`
      : "Answered without looking up data";
    turn.querySelector(".ask-turn__copy").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(finalText);
        toast("Answer copied.", "success");
      } catch {
        toast("Copy is not available in this browser.", "error");
      }
    });
  }

  return { handle, error, finish };
}
