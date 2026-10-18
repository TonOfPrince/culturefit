/* CultureFit: same-origin UI; provider credentials never enter the browser. */
(function (root) {
  "use strict";
  const LANES = ["place", "brand_partner", "artist"];
  const LABELS = {place: "A place", brand_partner: "A brand partner", artist: "An artist"};
  const MODES = ["SYNTHETIC_TRANSPORT_ONLY", "LIVE_HTTP_UNREVIEWED"];
  const text = (value, limit = 500) => typeof value === "string" && value.trim().length > 0 && value.length <= limit;
  function validPlan(data) {
    if (!data || data.product !== "CultureFit" || data.city !== "Chicago" ||
        !MODES.includes(data.execution_mode) || data.complete_required_lanes !== true ||
        data.provider_independently_verified !== false || data.public_release_authorized !== false ||
        data.submission_authorized !== false || !Array.isArray(data.resolved_seeds) ||
        data.resolved_seeds.length !== 1 || !data.resolved_seeds[0] || data.resolved_seeds[0].name !== "Taylor Swift" ||
        !text(data.resolved_seeds[0].entity_id) || !Array.isArray(data.evidence_cards) ||
        data.evidence_cards.length !== 3) return false;
    return LANES.every(lane => {
      const cards = data.evidence_cards.filter(card => card && card.lane === lane);
      const family = lane === "brand_partner" ? "brand" : lane;
      return cards.length === 1 && text(cards[0].name) && text(cards[0].entity_id) &&
        typeof cards[0].subtype === "string" &&
        (cards[0].subtype === "urn:entity:" + family ||
         cards[0].subtype.startsWith("urn:entity:" + family + ":"));
    });
  }
  function init(doc, fetcher) {
    const el = id => doc.getElementById(id);
    const run = el("plan-run"), status = el("plan-status"), results = el("plan-results");
    let submitting = false, attempted = false, connected = false, mode = null, statusPending = false;
    const make = (tag, value, className) => {
      const node = doc.createElement(tag);
      if (value !== undefined) node.textContent = String(value);
      if (className) node.className = className;
      return node;
    };
    function ready() { run.disabled = submitting || attempted || !connected; }
    async function jsonRequest(path, options) {
      const response = await fetcher(path, {...options, credentials: "same-origin", cache: "no-store"});
      if (!(response.headers.get("content-type") || "").toLowerCase().startsWith("application/json")) throw new Error("Unexpected response");
      const data = await response.json();
      return {response, data};
    }
    async function refresh() {
      if (statusPending || submitting) return;
      statusPending = true;
      connected = false; ready();
      try {
        const {response, data} = await jsonRequest("/api/provider-status");
        if (!response.ok || !data || typeof data.attempted !== "boolean" ||
            !["NETWORK_LOCKED", ...MODES].includes(data.mode) ||
            data.provider_independently_verified !== false) throw new Error("Unavailable");
        attempted = attempted || data.attempted;
        mode = data.mode; connected = MODES.includes(mode);
        el("provider-mode").textContent = mode === "SYNTHETIC_TRANSPORT_ONLY" ? "Offline demonstration" :
          mode === "LIVE_HTTP_UNREVIEWED" ? "Live Qloo connection" : "Provider locked";
        if (attempted) status.textContent = "This session has already used its single attempt. Results cannot be regenerated.";
        else if (!connected) status.textContent = "The provider is locked. An operator must configure the gateway before this example can run.";
        else status.textContent = mode === "SYNTHETIC_TRANSPORT_ONLY" ?
          "Ready. This uses clearly labelled fictional responses; it does not contact Qloo." :
          "Ready. This example will make one bounded Qloo request sequence.";
      } catch (_) {
        el("provider-mode").textContent = "Provider unavailable";
        status.textContent = "The provider connection is unavailable. Check the connection; no planning request was sent.";
      } finally { statusPending = false; ready(); }
    }
    function showPlan(data) {
      results.replaceChildren();
      for (const lane of LANES) {
        const card = data.evidence_cards.find(item => item.lane === lane);
        const article = make("article", undefined, "result-card");
        article.append(make("p", LABELS[lane], "eyebrow"), make("h3", card.name),
          make("p", lane === "place" ? "For the Chicago brief" : "Cultural affinity with the selected artist", "card-context"),
          make("p", data.execution_mode === "SYNTHETIC_TRANSPORT_ONLY" ?
            "Fictional demonstration result · not Qloo evidence" : "Returned by Qloo · aggregate affinity", "source"));
        results.append(article);
      }
      el("result-boundary").textContent = data.execution_mode === "SYNTHETIC_TRANSPORT_ONLY" ?
        "These are fictional demonstration records. No cultural recommendation or live provider verification is implied." :
        "These are provider-reported affinities, not endorsements or predictions. Confirm availability, local details and suitability before planning.";
      results.hidden = false;
    }
    async function plan(event) {
      event.preventDefault();
      if (submitting || attempted || !connected) return;
      submitting = true; attempted = true; ready();
      results.replaceChildren(); el("result-boundary").textContent = "";
      status.textContent = "Resolving the artist, then finding a place, a brand partner and an artist…";
      try {
        const {response, data} = await jsonRequest("/api/plan", {method: "POST",
          headers: {"Content-Type": "application/json"}, body: "{}"});
        if (!response.ok) {
          const messages = {409: "This session's single attempt has already been used.",
            422: "A complete, unambiguous brief could not be produced. No partial recommendations are shown.",
            503: "The provider is not configured or is locked."};
          status.textContent = messages[response.status] || "The attempt could not be completed. It will not be retried automatically.";
        } else if (!validPlan(data) || data.execution_mode !== mode) {
          status.textContent = "The response did not match this brief. No recommendations are shown and the attempt will not be repeated.";
        } else {
          showPlan(data); status.textContent = "Your three-part brief is ready. This session's single attempt is complete.";
        }
      } catch (_) {
        status.textContent = "The connection was interrupted. The attempt may have been consumed; no automatic retry will run.";
      } finally { submitting = false; ready(); }
    }
    el("plan-form").addEventListener("submit", plan);
    el("provider-refresh").addEventListener("click", refresh);

    const catalogStatus = el("catalog-status"), searchStatus = el("search-status");
    const searchButton = el("search-run"), uploadButton = el("upload-run");
    let catalogReady = false, catalogExists = false;
    function clearCatalogueResults() { el("catalog-results").replaceChildren(); }
    async function catalogueStatus() {
      try {
        const {response, data} = await jsonRequest("/api/catalog");
        catalogReady = response.ok && data.state === "ready";
        catalogExists = catalogReady || data.state !== "empty";
        catalogStatus.textContent = catalogReady ? data.record_count + " source records ready. Attribution is operator-supplied." :
          response.ok && data.state === "empty" ? "No catalogue yet. Import your own CSV to use these optional tools." :
          "The catalogue is unavailable. Check the local source file.";
      } catch (_) { catalogReady = false; catalogExists = true; catalogStatus.textContent = "Catalogue connection unavailable."; }
      searchButton.disabled = !catalogReady;
    }
    el("upload-form").addEventListener("submit", async event => {
      event.preventDefault();
      if (uploadButton.disabled) return;
      const file = el("catalog-file").files[0];
      if (!file || file.size > 2000000 || file.size === 0) { catalogStatus.textContent = "Choose a nonempty CSV file of at most 2 MB."; return; }
      if (catalogExists && !el("replace-catalog").checked) { catalogStatus.textContent = "Confirm replacement before overwriting the current catalogue."; return; }
      uploadButton.disabled = true; clearCatalogueResults();
      try {
        const headers = {"Content-Type": "text/csv"};
        if (el("replace-catalog").checked) headers["X-Replace-Catalogue"] = "yes";
        const {response} = await jsonRequest("/api/catalog", {method: "POST", headers, body: file});
        catalogStatus.textContent = response.ok ? "Catalogue imported." :
          response.status === 409 ? "Replacement was refused. Confirm replacement and choose the intended CSV." :
          "The CSV was refused. Check the header, records and source URLs.";
        if (response.ok) { el("replace-catalog").checked = false; await catalogueStatus(); }
      } catch (_) { catalogStatus.textContent = "The import connection was interrupted. Check catalogue status before trying again."; await catalogueStatus(); }
      finally { uploadButton.disabled = false; }
    });
    el("search-form").addEventListener("submit", async event => {
      event.preventDefault();
      if (!catalogReady || searchButton.disabled) return;
      searchButton.disabled = true; clearCatalogueResults();
      try {
        const {response, data} = await jsonRequest("/api/shortlist", {method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({city: el("city").value, interests: el("interests").value, per_lane: 3})});
        if (!response.ok || !data || data.provider_calls !== 0 || data.source_status !== "user_supplied_unverified" ||
            !data.lanes || !["place", "brand", "artist"].every(lane => Array.isArray(data.lanes[lane]))) {
          searchStatus.textContent = "No matches could be displayed. Check the catalogue and search terms."; return;
        }
        for (const lane of ["place", "brand", "artist"]) {
          const group = make("section", undefined, "result-card");
          group.append(make("h3", lane === "brand" ? "Brand partners" : lane === "place" ? "Places" : "Artists"));
          if (data.lanes[lane].length === 0) group.append(make("p", "No matching source records."));
          for (const card of data.lanes[lane]) {
            if (!card || !text(card.name) || card.source_status !== "user_supplied_unverified") throw new Error("Invalid source record");
            const row = make("div", undefined, "catalog-match");
            row.append(make("h4", card.name), make("p", "Source supplied by you: " + String(card.source_label || "Unlabelled")));
            // Source labels are text. Do not visit or embed operator URLs.
            group.append(row);
          }
          el("catalog-results").append(group);
        }
        searchStatus.textContent = "Matches use only your exact source tags. No provider request or source verification occurred.";
      } catch (_) { clearCatalogueResults(); searchStatus.textContent = "The search could not be completed. No matches are shown."; }
      finally { searchButton.disabled = !catalogReady; }
    });
    ready();
    const initial = Promise.all([refresh(), catalogueStatus()]);
    return {refresh, initial};
  }
  const api = {init, validPlan};
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root.document && typeof root.fetch === "function") init(root.document, root.fetch.bind(root));
})(typeof globalThis === "object" ? globalThis : this);
