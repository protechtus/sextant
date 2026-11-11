const DEFAULTS = {
  endpoint: "http://127.0.0.1:8766/api/inbox/conversation",
  apiKey: "",
};

async function load() {
  const cfg = await chrome.storage.local.get(DEFAULTS);
  document.getElementById("endpoint").value = cfg.endpoint || DEFAULTS.endpoint;
  document.getElementById("apiKey").value = cfg.apiKey || "";
}

document.getElementById("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    endpoint: document.getElementById("endpoint").value.trim() || DEFAULTS.endpoint,
    apiKey: document.getElementById("apiKey").value.trim(),
  });
  const s = document.getElementById("status");
  s.textContent = "saved";
  setTimeout(() => (s.textContent = ""), 2000);
});

load();
