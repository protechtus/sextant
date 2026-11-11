// Sextant Clip -- background service worker.
// Click the toolbar button: the content scraper runs in the page, the result
// POSTs to your local Sextant inbox. Nothing ever leaves your machine except
// the request to 127.0.0.1.

const DEFAULTS = {
  endpoint: "http://127.0.0.1:8766/api/inbox/conversation",
  apiKey: "",
};

async function settings() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

function badge(tabId, text, color) {
  chrome.action.setBadgeText({ tabId, text });
  chrome.action.setBadgeBackgroundColor({ tabId, color });
  setTimeout(() => chrome.action.setBadgeText({ tabId, text: "" }), 4000);
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || !tab.id) return;
  if (!/^https?:/.test(tab.url || "")) {
    // Browser-internal pages (chrome://, new tab, PDFs) cannot be scraped.
    // Say so -- a silent no-op reads as "the extension is broken"
    // (impatient-persona finding, quality loop run 4).
    badge(tab.id, "N/A", "#b45309");
    return;
  }
  const cfg = await settings();
  if (!cfg.apiKey) {
    badge(tab.id, "KEY?", "#b45309");
    chrome.runtime.openOptionsPage();
    return;
  }
  let capture;
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["scraper.js"],
    });
    capture = result;
  } catch (e) {
    badge(tab.id, "ERR", "#b91c1c");
    return;
  }
  if (!capture || !capture.messages || !capture.messages.length) {
    badge(tab.id, "0", "#b45309");
    return;
  }
  try {
    const resp = await fetch(cfg.endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg.apiKey,
      },
      body: JSON.stringify(capture),
    });
    if (resp.ok) {
      const data = await resp.json();
      badge(tab.id, String(data.turns || "OK"), "#15803d");
    } else {
      badge(tab.id, String(resp.status), "#b91c1c");
    }
  } catch (e) {
    badge(tab.id, "OFF?", "#b91c1c");
  }
});
