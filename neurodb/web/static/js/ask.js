// Ask NeuroDB: streams the assistant's answer (Server-Sent Events over a POST) into a conversation thread.
// The Ask page starts it by itself; another page's chat (Monitoring insights' Chat with Data, the Help
// assistant's panel: an element with data-module="ask", possibly swapped in by htmx) is started by app.js
// through init(). Such a chat may send its page's filter (data-scope) or the page it is on (data-page-context:
// the path and title) with each question, keep its own turn template inside it, name its parts with
// data-ask-part (form, input, thread, intro, submit, stop, new) instead of the Ask page's ids, show the
// day's quota the server sends (data-ask-quota), and keep its conversation for the browser tab
// (data-history-key: sessionStorage, so it follows the person from page to page; the server keeps the
// conversation itself and sends at most 6 earlier turns to the model).
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

const MAX_KEPT_TURNS = 20; // turns of a panel's conversation kept for the tab (the server sends at most 6)

function readKept(key) {
  if (!key) return null;
  try {
    const kept = JSON.parse(window.sessionStorage.getItem(key) || "null");
    return kept && typeof kept.conversation === "string" && Array.isArray(kept.turns) ? kept : null;
  } catch {
    return null; // storage unavailable or unreadable: start afresh
  }
}

function writeKept(key, kept) {
  if (!key) return;
  try {
    if (kept) window.sessionStorage.setItem(key, JSON.stringify(kept));
    else window.sessionStorage.removeItem(key);
  } catch {
    /* storage unavailable or full: the conversation lasts as long as the page */
  }
}

export function init(root) {
  const part = (name) => root.querySelector(`[data-ask-part="${name}"]`) ?? root.querySelector(`#ask-${name}`);
  const form = part("form");
  const input = part("input");
  const thread = part("thread");
  const intro = part("intro");
  const submit = part("submit");
  const stop = part("stop");
  const reset = part("new");
  const template = root.querySelector("template[data-ask-turn]") ?? document.querySelector("#ask-turn-template");
  const keptKey = root.dataset.historyKey || "";
  const kept = readKept(keptKey) ?? { conversation: newConversationId(), turns: [] };
  let conversation = kept.conversation;
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
    kept.conversation = conversation;
    kept.turns = [];
    writeKept(keptKey, null);
    thread.querySelectorAll(".ask-turn").forEach((el) => el.remove());
    intro.hidden = false;
    reset.hidden = true;
    input.focus();
  });

  // A panel's conversation of this tab, drawn again on the next page (answers as the server sanitized them)
  for (const turn of kept.turns) {
    const el = template.content.firstElementChild.cloneNode(true);
    el.querySelector(".ask-turn__question").textContent = turn.question;
    el.querySelector(".ask-turn__body").innerHTML = turn.html;
    thread.append(el);
  }
  if (kept.turns.length) {
    intro.hidden = true;
    reset.hidden = false;
  }

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
    if (root.dataset.pageContext !== undefined) {
      body.append("page", window.location.pathname);
      body.append("title", document.title);
    }
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
      await readEvents(res.body, (event) => {
        ui.handle(event);
        if (event.type === "done") {
          if (event.quota) {
            const chip = root.querySelector("[data-ask-quota]");
            if (chip) chip.textContent = event.quota;
          }
          if (keptKey) {
            kept.turns = [...kept.turns, { question, html: event.html }].slice(-MAX_KEPT_TURNS);
            writeKept(keptKey, kept);
          }
        }
      });
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
