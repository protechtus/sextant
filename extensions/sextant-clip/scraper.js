// Sextant Clip -- page scraper, injected on click (never runs otherwise).
// Best-effort per-provider DOM extraction with an honest fallback: if the
// site's markup has drifted (they all drift), you still get the page text as
// a single document turn rather than nothing.
(() => {
  const host = location.hostname;

  const clean = (el) => (el ? el.innerText.trim() : "");

  function fromNodes(nodes, roleFor) {
    const messages = [];
    nodes.forEach((n) => {
      const content = clean(n);
      if (!content) return;
      messages.push({ role: roleFor(n), content });
    });
    return messages;
  }

  function scrapeClaude() {
    // claude.ai: user turns and assistant turns carry data-testid markers.
    const nodes = document.querySelectorAll(
      '[data-testid="user-message"], [data-testid="assistant-message"], .font-user-message, .font-claude-message'
    );
    return fromNodes(nodes, (n) =>
      (n.getAttribute("data-testid") || n.className || "").includes("user")
        ? "user"
        : "assistant"
    );
  }

  function scrapeChatGPT() {
    // chatgpt.com: each turn article exposes data-message-author-role.
    const nodes = document.querySelectorAll("[data-message-author-role]");
    return fromNodes(nodes, (n) =>
      n.getAttribute("data-message-author-role") === "user" ? "user" : "assistant"
    );
  }

  function scrapeGemini() {
    // gemini.google.com: user queries and model responses use custom elements.
    const nodes = document.querySelectorAll("user-query, model-response");
    return fromNodes(nodes, (n) =>
      n.tagName.toLowerCase() === "user-query" ? "user" : "assistant"
    );
  }

  let messages = [];
  let source = "web";
  if (host.includes("claude.ai")) {
    source = "claude-web";
    messages = scrapeClaude();
  } else if (host.includes("chatgpt.com") || host.includes("chat.openai.com")) {
    source = "chatgpt-web";
    messages = scrapeChatGPT();
  } else if (host.includes("gemini.google.com")) {
    source = "gemini-web";
    messages = scrapeGemini();
  }

  if (!messages.length) {
    // Fallback: whole visible page as one document turn. Better a lossless
    // blob in your corpus than a perfect schema you didn't capture.
    const text = (document.body ? document.body.innerText : "").trim();
    if (text) {
      source = source === "web" ? "web-page" : source + "-fallback";
      messages = [{ role: "user", content: text.slice(0, 400000) }];
    }
  }

  return {
    title: document.title || "chat",
    source,
    url: location.href,
    messages,
  };
})();
