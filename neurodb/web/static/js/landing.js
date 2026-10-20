// Public landing page (landing.html): the hero photo, the story explorer, the sample AI insights, the role
// views, the mobile menu, the scroll meter, reveal-on-scroll and the counting figures. It shows no word of
// its own: every text comes from the page (its <template>s and data attributes), so it is translated with
// the template, and nothing is fetched. It imports nothing (the page does not extend base.html, so it has
// no import map). Under prefers-reduced-motion nothing moves: no reveal, no counting.
const root = document.documentElement;
const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));

/** Copy each field ([data-f]) of a state (a <template>'s content) into the field of the same name in
 * ``region``: its children (cloned, so the state can be used again) and, for a link, its address. */
function apply(state, region) {
  for (const source of $$("[data-f]", state)) {
    const target = $(`[data-f="${source.dataset.f}"]`, region);
    if (!target) continue;
    target.replaceChildren(...Array.from(source.childNodes, (node) => node.cloneNode(true)));
    if (source.hasAttribute("href")) target.setAttribute("href", source.getAttribute("href"));
  }
}

const template = (selector) => document.querySelector(`template${selector}`)?.content ?? null;

// ------------------------------------------------------------------------------------------ hero photo
const heroArt = $("#heroArt");
const heroPhoto = $("#heroPhoto");
if (heroArt && heroPhoto) {
  const loaded = () => heroArt.classList.add("photo-loaded");
  heroPhoto.addEventListener("load", loaded);
  heroPhoto.addEventListener("error", () => heroArt.classList.remove("photo-loaded"));
  if (heroPhoto.complete && heroPhoto.naturalWidth) loaded();
}
// a photo that cannot load leaves its panel's colour, as the design's inline handler did; one that failed
// before this module ran (complete, with no width) is hidden at once. A lazy photo not yet asked for is not
// complete, so it waits for its own error.
for (const img of $$("img[data-hide-on-error]")) {
  const hide = () => {
    img.style.display = "none";
  };
  if (img.complete && img.naturalWidth === 0 && img.currentSrc) hide();
  else img.addEventListener("error", hide);
}

/** Once the first state is drawn, ``region`` says its later changes to screen readers (politely): drawing
 * the first one is not announced. */
const speak = (region) => region?.setAttribute("aria-live", "polite");

// ------------------------------------------------------------------------------------------ the story
const board = $("#storyboard");
if (board) {
  // The picture is the photo (its alt, caption and credit are read) or an illustration (aria-hidden); the
  // story's text (#storyText) says each stage.
  const canvas = $("#visualCanvas");
  const scenarios = $$("button[data-scenario]");
  const steps = $$(".stepbtn[data-step]", board);
  const prev = $("#prev");
  const next = $("#next");
  const overline = $("#stageBottom");
  let active = "learning";
  let step = 0;

  const render = () => {
    const state = template(`[data-story-state="${active}"][data-story-stage="${step}"]`);
    if (state) apply(state, board);
    const visual =
      step === 0 ? template(`[data-story-photo="${active}"]`) : template(`[data-story-art="${step}"]`);
    canvas.replaceChildren(...(visual ? [visual.cloneNode(true)] : []));
    canvas.dataset.scenario = active;
    const button = scenarios.find((b) => b.dataset.scenario === active);
    for (const number of $$("[data-stage-no]", board)) number.textContent = String(step + 1).padStart(2, "0");
    // Back, pressed back to the first stage, is disabled: keyboard focus moves to Next first, not to the page
    if (step === 0 && document.activeElement === prev) next.focus();
    prev.disabled = step === 0;
    next.textContent = step === steps.length - 1 ? next.dataset.restart : next.dataset.next;
    steps.forEach((el, i) => {
      el.classList.toggle("active", i === step);
      if (i === step) el.setAttribute("aria-current", "step");
      else el.removeAttribute("aria-current");
    });
    if (button?.dataset.overline) overline.textContent = button.dataset.overline;
  };

  for (const button of scenarios) {
    button.addEventListener("click", () => {
      active = button.dataset.scenario;
      step = 0;
      for (const b of scenarios) b.setAttribute("aria-pressed", String(b === button));
      render();
    });
  }
  steps.forEach((el, i) =>
    el.addEventListener("click", () => {
      step = i;
      render();
    }),
  );
  prev.addEventListener("click", () => {
    step = Math.max(0, step - 1);
    render();
  });
  next.addEventListener("click", () => {
    step = (step + 1) % steps.length;
    render();
  });
  render();
  speak($("#storyText"));
}

// ------------------------------------------------------------------------------------------ the roles
const rolePanel = $("#rolePanel");
if (rolePanel) {
  const cards = $$(".rolecard[data-role]");
  const show = (key) => {
    const state = template(`[data-role-state="${key}"]`);
    if (state) apply(state, rolePanel);
    for (const card of cards) card.setAttribute("aria-pressed", String(card.dataset.role === key));
  };
  for (const card of cards) card.addEventListener("click", () => show(card.dataset.role));
  show("staff");
  speak($("#roleText"));
}

// ------------------------------------------------------------------------------------------ sample insights
const lab = $("#insights");
if (lab) {
  const tabs = $$(".insight-tab[data-insight]", lab);
  const toggle = $("#evidenceToggle");
  const detail = $("#insightEvidenceDetail");
  const show = (key) => {
    const state = template(`[data-insight-state="${key}"]`);
    if (!state) return;
    for (const tab of tabs) tab.setAttribute("aria-pressed", String(tab.dataset.insight === key));
    apply(state, lab);
    detail.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  };
  for (const tab of tabs) tab.addEventListener("click", () => show(tab.dataset.insight));
  toggle.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    toggle.setAttribute("aria-expanded", String(!detail.hidden));
  });
  show("delivery");
}

// ------------------------------------------------------------------------------------------ mobile menu
const menu = $("#mobileMenu");
const navLinks = $("#navLinks");
if (menu && navLinks) {
  const close = () => {
    navLinks.classList.remove("open");
    menu.setAttribute("aria-expanded", "false");
  };
  menu.addEventListener("click", () => {
    const open = navLinks.classList.toggle("open");
    menu.setAttribute("aria-expanded", String(open));
  });
  for (const link of $$("a", navLinks)) link.addEventListener("click", close);
  // Tab past the last link (or Shift+Tab before the button) closes it; a click on nothing focusable does not
  const leave = (event) => {
    const to = event.relatedTarget;
    if (to && to !== menu && !navLinks.contains(to)) close();
  };
  menu.addEventListener("focusout", leave);
  navLinks.addEventListener("focusout", leave);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && navLinks.classList.contains("open")) {
      close();
      menu.focus();
    }
  });
}

// ------------------------------------------------------------------------------------------ motion
// All content is drawn with the page; motion only enhances what scrolls into view.
const meter = document.createElement("div");
meter.className = "scroll-meter";
meter.setAttribute("aria-hidden", "true");
meter.append(document.createElement("i"));
document.body.append(meter);
const updateMeter = () => {
  const height = Math.max(1, root.scrollHeight - window.innerHeight);
  const share = Math.max(0, Math.min(1, window.scrollY / height));
  meter.firstElementChild.style.width = `${(share * 100).toFixed(2)}%`;
};
window.addEventListener("scroll", updateMeter, { passive: true });
window.addEventListener("resize", updateMeter, { passive: true });
updateMeter();

const revealed = $$(".data-reveal");
const moving = !reduceMotion && "IntersectionObserver" in window;
if (moving) {
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) entry.target.classList.toggle("is-visible", entry.isIntersecting);
    },
    { threshold: 0.08, rootMargin: "0px 0px -4% 0px" },
  );
  revealed.forEach((el) => observer.observe(el));
  requestAnimationFrame(() => root.classList.add("motion-ready"));
} else {
  revealed.forEach((el) => el.classList.add("is-visible"));
}

// Counting figures: the live public highlights ([data-count], aggregate counts) and the figures of the
// platform's rules ([data-tally]). Each counts up once, when it comes into view; with motion reduced the
// number written in the page stays as it is.
if (moving) {
  const format = new Intl.NumberFormat(root.lang || undefined);
  const countUp = (el) => {
    const goal = Number(el.dataset.count ?? el.dataset.tally);
    if (!Number.isFinite(goal) || goal <= 0) return;
    const start = performance.now();
    const paint = (now) => {
      const t = Math.min(1, (now - start) / 930);
      el.textContent = format.format(Math.round(goal * (1 - (1 - t) ** 3)));
      if (t < 1) requestAnimationFrame(paint);
    };
    requestAnimationFrame(paint);
  };
  const counter = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        counter.unobserve(entry.target);
        countUp(entry.target);
      }
    },
    { threshold: 0.6 },
  );
  $$("[data-count], [data-tally]").forEach((el) => counter.observe(el));
}
