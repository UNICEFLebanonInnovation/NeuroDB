// Ask NeuroDB: streams the assistant's answer (Server-Sent Events over a POST) into a conversation thread.
// The Ask page starts it by itself; another page's chat (Monitoring insights' Chat with Data, an element
// with data-module="ask", possibly swapped in by htmx) is started by app.js through init(). Such a chat
// may send its page's filter (data-scope) with each question, and keep its own turn template inside it.
import { init as drawChart } from "./charts.js";
import { csrfToken, toast } from "./lib.js";

let chartCount = 0;

const page = document.querySelector("[data-ask]:not([data-module])");
if (page) init(page);

function newConversationId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
    (c ^ (window.crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16),
  );
}

export function init(root) {
  const form = root.querySelector("#ask-form");
  const input = root.querySelector("#ask-input");
  const thread = root.querySelector("#ask-thread");
  const intro = root.querySelector("#ask-intro");
  const submit = root.querySelector("#ask-submit");
  const stop = root.querySelector("#ask-stop");
  const reset = root.querySelector("#ask-new");
  const template = root.querySelector("template[data-ask-turn]") ?? document.querySelector("#ask-turn-template");
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
    if (root.dataset.scope !== undefined) body.append("scope", root.dataset.scope);
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

  // A question from /ask/?q= (the search box's link) is only filled in: the user presses Ask, so a
  // crawler following the link or a page reload never sends it.
  autosize();
  if (root.dataset.module) return; // a chat inside another page: never take the focus (or scroll) on load
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
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
      if (event.notice) {
        // what the server's check of the answer found (visit references, figures)
        const notice = document.createElement("p");
        notice.className = "source-line";
        notice.textContent = event.notice;
        bodyEl.append(notice);
      }
      finalText = event.answer;
      answered = true;
    } else if (event.type === "chart") {
      chart(event.spec);
    } else if (event.type === "error") {
      error(event.message);
    }
  }

  // A chart the assistant drew from figures it looked up (checked on the server), with the
  // dashboards' chart code.
  function chart(spec) {
    chartCount += 1;
    const id = `ask-chart-${chartCount}`;
    const figure = document.createElement("figure");
    figure.className = "ask-chart panel";
    const caption = document.createElement("figcaption");
    caption.className = "ask-chart__title";
    caption.textContent = spec.unit ? `${spec.title} (${spec.unit})` : spec.title;
    const data = document.createElement("script");
    data.type = "application/json";
    data.id = id;
    data.textContent = JSON.stringify(spec.data);
    const el = document.createElement("div");
    el.className = "chart";
    el.dataset.chart = spec.chart;
    el.dataset.source = id;
    el.dataset.orientation = spec.orientation;
    el.dataset.height = spec.chart === "bars" ? "" : "300";
    el.setAttribute("aria-label", spec.title);
    figure.append(caption, data, el);
    turn.querySelector(".ask-turn__charts").append(figure);
    drawChart(el).catch(() => {
      el.textContent = "The chart could not be drawn.";
    });
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
