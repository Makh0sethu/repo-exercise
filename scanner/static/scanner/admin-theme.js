// Light/dark switch for the Django admin: same storage key and attribute as the app (see app.js).
const toggle = document.getElementById("theme-toggle");
if (toggle) {
  const sync = () => toggle.setAttribute("aria-pressed", String(document.documentElement.dataset.theme === "dark"));
  sync();
  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("sentinel-theme", next); } catch (e) {}
    sync();
  });
}
