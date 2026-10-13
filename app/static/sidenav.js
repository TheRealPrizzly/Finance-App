/* Section sidebar: highlights the section being read and keeps jumps clear of the
   sticky top bar. The links are plain anchors, so they work without this script. */
(() => {
  "use strict";

  const nav = document.querySelector(".sidenav");
  if (!nav) return;
  const root = document.documentElement;
  const topbar = document.querySelector(".topbar");

  // Pairs of [link, section], in page order. Links to sections the page didn't render are hidden.
  const pairs = [];
  for (const link of nav.querySelectorAll('a[href^="#"]')) {
    const target = document.getElementById(decodeURIComponent(link.hash.slice(1)));
    if (target) pairs.push([link, target]);
    else link.closest("li").hidden = true;
  }
  if (!pairs.length) return;

  // On narrower screens the sidebar is a row of chips stuck under the top bar instead of a column.
  const asChips = () => getComputedStyle(nav.parentElement).display === "block";

  // Anchor jumps stop below the top bar (taller when it wraps on phones) and the chip row.
  function setOffset() {
    const bar = topbar?.offsetHeight || 0;
    root.style.setProperty("--topbar-height", `${bar}px`);
    root.style.scrollPaddingTop = `${bar + (asChips() ? nav.offsetHeight : 0) + 16}px`;
  }

  // The current section is the last one whose top has scrolled up to the offset line,
  // or the last one once the page can't scroll any further.
  let current = null;
  function update() {
    const line = (parseFloat(getComputedStyle(root).scrollPaddingTop) || 0) + 8;
    let active = pairs[0][0];
    for (const [link, target] of pairs) {
      if (target.getBoundingClientRect().top <= line) active = link;
    }
    if (window.innerHeight + window.scrollY >= root.scrollHeight - 2) active = pairs[pairs.length - 1][0];
    if (asChips()) active = active.closest(".sidenav > ul > li").querySelector("a");  // chips have no sub-sections
    if (active === current) return;
    current?.removeAttribute("aria-current");
    active.setAttribute("aria-current", "location");
    current = active;
    if (asChips()) nav.scrollTo({ left: active.offsetLeft - 16, behavior: "smooth" });
  }

  // Sections fill in as their data loads, pushing the ones below down. After a jump (a sidebar
  // link, or an address ending in #section) keep that section in view until the reader scrolls.
  const byHash = (hash) => (hash.length > 1 ? document.getElementById(decodeURIComponent(hash.slice(1))) : null);
  let pinned = byHash(location.hash);
  nav.addEventListener("click", (e) => {
    const link = e.target.closest('a[href^="#"]');
    if (link) pinned = byHash(link.hash);
  });
  for (const type of ["wheel", "touchstart", "pointerdown", "keydown"]) {
    window.addEventListener(type, (e) => { if (!nav.contains(e.target)) pinned = null; }, { passive: true });
  }
  function relayout() {
    pinned?.scrollIntoView({ block: "start" });
    update();
  }

  setOffset();
  update();
  window.addEventListener("scroll", update, { passive: true });  // fired at most once per frame
  window.addEventListener("resize", () => { setOffset(); update(); });
  if (window.ResizeObserver) new ResizeObserver(relayout).observe(document.querySelector(".main") || document.body);
})();
