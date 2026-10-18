/* Explicit offline hosting adapter. No Qloo credential or live transport. */
(function (root) {
  "use strict";
  if (root.location.hostname === "127.0.0.1" || root.location.hostname === "localhost" ||
      root.document.documentElement.getAttribute("data-hosted-demo") !== "true") return;
  const originalFetch = root.fetch.bind(root);
  const prefix = "culturefit-demo-v1:";
  let memory = {};
  function read(key) {
    try { return root.sessionStorage.getItem(prefix + key) || ""; }
    catch (_) { return memory[key] || ""; }
  }
  function write(key, value) {
    memory[key] = value;
    try { root.sessionStorage.setItem(prefix + key, value); } catch (_) {}
  }
  const jsonResponse = (status, data) => new Response(JSON.stringify(data),
    {status, headers: {"Content-Type": "application/json"}});
  root.fetch = async function (path, options = {}) {
    const method = options.method || "GET";
    if (path === "/api/provider-status" && method === "GET") {
      return jsonResponse(200, {mode:"SYNTHETIC_TRANSPORT_ONLY", attempted:read("attempted") === "yes", provider_independently_verified:false});
    }
    if (!["/api/catalog", "/api/shortlist", "/api/plan"].includes(path)) {
      return originalFetch(path, options);
    }
    if (path === "/api/plan") {
      if (read("attempted") === "yes") return jsonResponse(409, {error:"provider_already_attempted"});
      write("attempted", "yes");
    }
    let body = "";
    if (method === "POST") body = typeof options.body === "string" ? options.body : await options.body.text();
    const headers = new Headers(options.headers || {});
    const payload = {path, method, body, catalogue_csv:read("catalogue"), replace_confirmed:headers.get("X-Replace-Catalogue") === "yes"};
    const response = await originalFetch("/api/demo", {method:"POST",credentials:"same-origin",cache:"no-store",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    if (path === "/api/catalog" && method === "POST" && response.ok) write("catalogue", body);
    return response;
  };
})(window);
