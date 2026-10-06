const labels = {
  world: ["World", "✺"], economy: ["Economy", "¤"],
  ai_technology: ["AI & Technology", "✧"], education: ["Education", "✦"],
  politics: ["Politics", "✳"]
};
let state;
const $ = (id) => document.getElementById(id);

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function render() {
  $("today").textContent = new Intl.DateTimeFormat("en", { weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(new Date()).toUpperCase();
  const country = $("country");
  for (const value of state.countries) country.add(new Option(value, value, false, value === state.home_country));
  country.onchange = () => { state.home_country = country.value; updateFolio(); };

  const regions = $("regions");
  for (const value of state.region_options) {
    const button = element("button", "chip", value);
    button.type = "button";
    button.setAttribute("aria-pressed", state.regions.includes(value));
    button.onclick = () => {
      state.regions = state.regions.includes(value) ? state.regions.filter(x => x !== value) : [...state.regions, value];
      button.setAttribute("aria-pressed", state.regions.includes(value));
    };
    regions.append(button);
  }

  const topics = $("topics");
  for (const [key, [name, icon]] of Object.entries(labels)) {
    const card = element("button", "topic-card");
    card.type = "button";
    card.setAttribute("aria-pressed", state.enabled_topics.includes(key));
    card.append(element("span", "topic-icon", icon), element("span", "topic-title", name), element("span", "topic-check", state.enabled_topics.includes(key) ? "✓" : ""));
    card.onclick = () => {
      state.enabled_topics = state.enabled_topics.includes(key) ? state.enabled_topics.filter(x => x !== key) : [...state.enabled_topics, key];
      card.setAttribute("aria-pressed", state.enabled_topics.includes(key));
      card.querySelector(".topic-check").textContent = state.enabled_topics.includes(key) ? "✓" : "";
      updateFolio();
    };
    topics.append(card);
  }

  const publishers = new Map();
  for (const feeds of Object.values(state.feeds)) for (const feed of feeds) {
    if (!publishers.has(feed.source)) publishers.set(feed.source, []);
    publishers.get(feed.source).push(feed);
  }
  const sources = $("sources");
  for (const [name, feeds] of publishers) {
    const row = element("div", "source-row");
    const copy = element("div");
    copy.append(element("div", "source-name", name), element("div", "source-meta", feeds.length + " DESKS AVAILABLE"));
    const label = element("label");
    const toggle = element("input");
    toggle.type = "checkbox";
    toggle.setAttribute("aria-label", "Use " + name);
    toggle.checked = feeds.some(feed => feed.enabled !== false);
    toggle.onchange = () => { for (const feed of feeds) feed.enabled = toggle.checked; updateFolio(); };
    label.append(toggle);
    row.append(copy, label);
    sources.append(row);
  }

  $("stories").textContent = state.max_stories_per_topic;
  $("minus").onclick = () => changeStories(-1);
  $("plus").onclick = () => changeStories(1);
  $("start-time").value = state.start_time;
  $("start-time").onchange = () => { state.start_time = $("start-time").value; updateFolio(); };
  for (const name of state.voice_options) $("voice").add(new Option(name.replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase()), name, false, name === state.voice));
  $("voice").onchange = () => state.voice = $("voice").value;
  for (const name of state.models) $("model").add(new Option(name, name, false, name === state.ollama_model));
  $("model").onchange = () => state.ollama_model = $("model").value;
  $("save").onclick = save;
  updateFolio();
}

function changeStories(delta) {
  state.max_stories_per_topic = Math.max(1, Math.min(10, state.max_stories_per_topic + delta));
  $("stories").textContent = state.max_stories_per_topic;
}

function updateFolio() {
  $("folio-country").textContent = state.home_country;
  $("folio-topics").textContent = state.enabled_topics.length;
  $("folio-sources").textContent = new Set(Object.values(state.feeds).flat().filter(f => f.enabled !== false).map(f => f.source)).size;
  $("folio-time").textContent = state.start_time;
}

async function save() {
  const status = $("status");
  const enabledFeeds = [...new Set(Object.values(state.feeds).flat().filter(feed => feed.enabled !== false).map(feed => feed.url))];
  const payload = {
    home_country: state.home_country, regions: state.regions,
    enabled_topics: state.enabled_topics, enabled_feeds: enabledFeeds,
    start_time: state.start_time, voice: state.voice,
    ollama_model: state.ollama_model, max_stories_per_topic: state.max_stories_per_topic
  };
  status.textContent = "Saving your edition…";
  $("save").disabled = true;
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-MorningBird-Token": state.csrf },
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not save settings.");
    status.textContent = "Edition saved. Your next briefing will use these choices.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    $("save").disabled = false;
  }
}

fetch("/api/settings").then(response => {
  if (!response.ok) throw new Error("Cannot load your settings.");
  return response.json();
}).then(data => { state = data; render(); }).catch(error => {
  $("status").textContent = error.message;
});

fetch("/api/frontpage").then(response => response.json()).then(stories => {
  const wire = $("wire-stories");
  wire.replaceChildren();
  if (!stories.length) {
    wire.append(element("p", "wire-loading", "Current photo stories are unavailable. Your selected RSS sources still power the briefing."));
    return;
  }
  for (const story of stories) {
    const link = element("a", "wire-story");
    link.href = story.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const photo = element("img");
    photo.src = story.image;
    photo.alt = "Publisher photo for: " + story.title;
    photo.loading = "lazy";
    link.append(photo, element("span", "wire-source", story.source), element("strong", "wire-title", story.title));
    wire.append(link);
  }
}).catch(() => {
  $("wire-stories").textContent = "Current photo stories are unavailable. Your selected RSS sources still power the briefing.";
});
