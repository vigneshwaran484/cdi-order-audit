// Salesman order screen. Orders go into a localStorage queue first, then the
// queue is sent to /api/orders. If the phone is offline (or the request
// fails), the order stays queued and is retried when the network returns.
// Each order has a client_ref made here, so a retry never duplicates it.
(function () {
  const KEY = "order_queue_v1";
  const $ = (s) => document.querySelector(s);
  const qtyInputs = [...document.querySelectorAll("[data-qty]")];
  $("#odate").value = new Date().toISOString().slice(0, 10);

  const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
    : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12));
  const load = () => JSON.parse(localStorage.getItem(KEY) || "[]");
  const save = (q) => localStorage.setItem(KEY, JSON.stringify(q));
  const custName = (id) => (ORDER_DATA.customers.find((c) => c.id == id) || {}).name;

  function amount() {
    let paise = 0;
    for (const i of qtyInputs) paise += (parseInt(i.value) || 0) * Math.round(parseFloat(i.dataset.price) * 100);
    $("#amt").textContent = (paise / 100).toFixed(2);
  }
  document.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => {
    const input = document.querySelector(`[data-qty="${b.dataset.id}"]`);
    input.value = Math.max(0, (parseInt(input.value) || 0) + parseInt(b.dataset.step));
    amount();
  }));
  qtyInputs.forEach((i) => i.addEventListener("input", amount));

  function show(html, kind) { $("#msg").innerHTML = `<div class="flash ${kind || ""}">${html}</div>`; }

  function renderQueue() {
    const q = load();
    $("#queuebox").hidden = q.length === 0;
    $("#queue").innerHTML = q.map((o) =>
      `<li>${custName(o.customer_id)}: ${o.lines.map((l) => l.qty).reduce((a, b) => a + b, 0)} packets <span class="muted small">(${o.order_date})</span></li>`).join("");
    const net = $("#net");
    net.className = "net " + (navigator.onLine ? "on" : "off");
    net.textContent = navigator.onLine ? "Online" : "Offline: orders will be saved on this phone";
  }

  let flushing = false;
  async function flush() {
    if (flushing) return;
    flushing = true;
    try {
      for (const o of load()) {
        let res;
        try {
          res = await fetch("/api/orders", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify(o), credentials: "same-origin" });
        } catch (e) { break; }                       // still offline: keep everything queued
        if (res.status === 401) { show("Session expired. <a href='/login'>Log in again</a>; your orders are kept.", "error"); break; }
        if (res.ok) {
          const data = await res.json();
          save(load().filter((x) => x.client_ref !== o.client_ref));
          show(`Order #${data.id} for ${custName(o.customer_id)} sent to the factory.`);
        } else if (res.status === 422) {
          const data = await res.json().catch(() => ({}));
          save(load().filter((x) => x.client_ref !== o.client_ref));
          show(`Order for ${custName(o.customer_id)} was refused: ${data.error || "invalid"}`, "error");
        } else { break; }                            // server trouble: retry later
      }
    } finally { flushing = false; renderQueue(); }
  }

  $("#submit").addEventListener("click", () => {
    const customer_id = parseInt($("#customer").value);
    const lines = qtyInputs.filter((i) => parseInt(i.value) > 0)
      .map((i) => ({ item_id: parseInt(i.dataset.qty), qty: parseInt(i.value), unit: "pkt" }));
    if (!customer_id) return show("Choose a customer.", "error");
    if (!lines.length) return show("Add at least one product.", "error");
    const q = load();
    q.push({ client_ref: uuid(), customer_id, order_date: $("#odate").value, lines });
    save(q);
    qtyInputs.forEach((i) => (i.value = 0)); amount();
    show(navigator.onLine ? "Sending…" : "Saved on this phone. It will be sent when you are back online.");
    renderQueue();
    flush();
  });

  window.addEventListener("online", flush);
  window.addEventListener("offline", renderQueue);
  setInterval(flush, 20000);
  renderQueue(); flush();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
})();
