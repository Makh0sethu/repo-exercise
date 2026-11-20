try {
  if (localStorage.getItem("sentinel-theme") === "dark") {
    document.documentElement.dataset.theme = "dark";
  }
} catch (e) {}
