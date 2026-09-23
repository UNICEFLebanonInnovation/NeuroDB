// Landing page behaviour: theme toggle, sticky nav shadow, reveal-on-scroll, counting figures.
const root = document.documentElement;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const toggle = document.getElementById("theme-toggle");
if (toggle) {
  toggle.setAttribute("aria-pressed", String(root.getAttribute("data-bs-theme") === "dark"));
  toggle.addEventListener("click", () => {
    const next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-bs-theme", next);
    toggle.setAttribute("aria-pressed", String(next === "dark"));
    try {
      localStorage.setItem("neurodb-theme", next);
    } catch {
      /* storage unavailable */
    }
  });
}

const nav = document.getElementById("lp-nav");
const onScroll = () => nav?.classList.toggle("is-scrolled", window.scrollY > 8);
window.addEventListener("scroll", onScroll, { passive: true });
onScroll();

const format = new Intl.NumberFormat();
function countUp(el) {
  const target = Number(el.dataset.count);
  if (!Number.isFinite(target) || reduceMotion || target === 0) return;
  const start = performance.now();
  const duration = 1200;
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    el.textContent = format.format(Math.round(target * (1 - (1 - t) ** 3)));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

const revealables = document.querySelectorAll(".lp-reveal");
if ("IntersectionObserver" in window) {
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add("is-visible");
        entry.target.querySelectorAll("[data-count]").forEach(countUp);
        observer.unobserve(entry.target);
      }
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.12 },
  );
  revealables.forEach((el) => observer.observe(el));
} else {
  revealables.forEach((el) => el.classList.add("is-visible"));
}
