const toggle = document.querySelector("[data-filter-toggle]");
const panel = document.querySelector("#library-filters");

if (toggle && panel) {
  toggle.addEventListener("click", () => {
    const open = panel.classList.toggle("filter-panel--open");
    toggle.setAttribute("aria-expanded", String(open));
  });
}
