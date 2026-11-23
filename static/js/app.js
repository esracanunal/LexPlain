/* LexPlain ön yüzü: saf JavaScript (çerçeve yok). */
"use strict";

/* ================================================================ durum */
const state = {
  meta: null,
  source: "file",          // "file" | "paste"
  file: null,
  typeTouched: false,      // kullanıcı türü kendisi seçtiyse otomatik öneri üzerine yazılmaz
  report: null,
  lastRequest: null,       // "Yeniden incele" / "Tekrar dene" için
  chat: [],
  filter: "all",
  openFindingId: null,
  docReturnFocus: null,
};

const TYPE_NAMES = { kira: "Kira sözleşmesi", is: "İş sözleşmesi", hizmet: "Abonelik / hizmet sözleşmesi", diger: "Diğer sözleşme" };
const ROLE_OPTIONS = {
  kira: [["kiraci", "Kiracı", "Evi ya da daireyi kiralayan"], ["kiraya_veren", "Kiraya veren", "Mülkü kiraya veren (ev sahibi)"]],
  is: [["calisan", "Çalışan", "İş, staj ya da yarı zamanlı çalışan"], ["isveren", "İşveren", "Personeli çalıştıran kişi ya da şirket"]],
  hizmet: [["musteri", "Müşteri", "Abonelik, üyelik ya da hizmet alan"], ["firma", "Hizmet veren", "Hizmeti sunan firma"]],
};
const SAMPLE_PRESET = { kira: ["kira", "kiraci"], is: ["is", "calisan"], hizmet: ["hizmet", "musteri"] };
const WEAK_ROLES = new Set(["kiraci", "musteri", "calisan", "taraf"]);
const RISK_LABEL = { yuksek: "Yüksek risk", orta: "Orta risk", dusuk: "Düşük risk" };
const RISK_SHORT = { yuksek: "yüksek", orta: "orta", dusuk: "düşük" };
const RISK_RANK = { yuksek: 0, orta: 1, dusuk: 2 };
const LEGAL_STATUS = {
  gecersiz_olabilir: "Bu madde yasaya aykırı olabilir; imzalasanız bile sizi bağlamayabilir.",
  gecerli_ama_riskli: "Madde geçerli olabilir, ancak aleyhinize sonuçlar doğurabilir.",
  belirsiz: "Geçerliliği, sözleşmenin tamamına ve somut duruma göre değişebilir.",
};
const REQUEST_TIMEOUT_MS = 210000;

/* ================================================================ yardımcılar */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

/** Güvenli DOM üretimi: metin her zaman metin düğümü olarak eklenir. */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const icon = (name) => el("i", { class: `bi ${name}`, "aria-hidden": "true" });

let toastTimer = null;
function toast(message) {
  const t = $("#toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

async function copyText(text, done = "Kopyalandı") {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch {
    toast("Kopyalanamadı. Metni seçip elle kopyalayın.");
  }
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(path, { ...options, signal: ctrl.signal });
  } catch (err) {
    if (err.name === "AbortError") throw new ApiError("İnceleme beklenenden uzun sürdü ve durduruldu. Lütfen tekrar deneyin.", 0);
    throw new ApiError("LexPlain'e şu an ulaşılamıyor. İnternet bağlantınızı kontrol edip tekrar deneyin.", 0);
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try { body = await res.json(); } catch { /* boş gövde */ }
  if (!res.ok) {
    let msg = "Bir sorun oluştu. Lütfen tekrar deneyin.";
    if (body && typeof body.detail === "string") msg = body.detail;
    else if (body && Array.isArray(body.detail)) msg = "Gönderilen bilgilerde eksik ya da hatalı bir alan var.";
    throw new ApiError(msg, res.status);
  }
  return body;
}

const fmtTL = (n) => new Intl.NumberFormat("tr-TR", { style: "currency", currency: "TRY", maximumFractionDigits: 0 }).format(n);
const fmtDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("tr-TR", { day: "numeric", month: "long", year: "numeric" });
const fmtSize = (b) => (b < 1024 * 1024 ? `${Math.max(1, Math.round(b / 1024))} KB` : `${(b / 1024 / 1024).toFixed(1).replace(".", ",")} MB`);
const num = (id) => parseFloat(String($(id).value).replace(",", "."));
const firstSentences = (text, n) => (text.match(/[^.!?]+[.!?]+(\s|$)/g) || [text]).slice(0, n).join("").trim();
const lowerTr = (s) => s.toLocaleLowerCase("tr");

/** "MADDE 5 - Kiralananda…" → "Madde 5" */
function shortTitle(t) {
  let s = (t || "").split(/\s[-–:]\s|\.\s|\)\s/)[0].trim();
  if (/^\d+$/.test(s)) s = `Madde ${s}`;
  s = s.replace(/^MADDE/i, "Madde");
  return s.length > 24 ? s.slice(0, 22) + "…" : s;
}

function showView(name) {
  for (const v of ["Start", "Loading", "Results"]) $(`#view${v}`).hidden = v !== name;
}

/* ================================================================ başlangıç */
async function init() {
  bindSource();
  bindTypeAndRole();
  bindNavigation();
  bindCatchTabs();
  bindResults();
  bindCalculators();
  bindDocPanel();
  $("#composer").addEventListener("submit", (ev) => { ev.preventDefault(); submitAnalysis(); });
  $("#retryBtn").addEventListener("click", () => submitAnalysis());
  try {
    state.meta = await api("/api/meta");
    $("#maxMb").textContent = state.meta.max_mb;
  } catch { /* meta olmadan da çalışır */ }
}

/* ================================================================ kaynak: dosya / metin */
function setSource(src, focus = false) {
  state.source = src;
  for (const tab of $$(".lx-tab-mini")) {
    const on = tab.dataset.source === src;
    tab.setAttribute("aria-selected", on);
    tab.tabIndex = on ? 0 : -1;
    if (on && focus) tab.focus();
  }
  $("#sourceFile").hidden = src !== "file";
  $("#sourcePaste").hidden = src !== "paste";
  clearFieldErrors();
}

function setFile(f) {
  state.file = f || null;
  $("#dropZone").hidden = !!f;
  $("#fileRow").hidden = !f;
  if (f) {
    $("#fileName").textContent = f.name;
    $("#fileMeta").textContent = fmtSize(f.size);
  } else {
    $("#fileInput").value = "";
  }
  fieldError("file", null);
}

function bindSource() {
  for (const tab of $$(".lx-tab-mini")) tab.addEventListener("click", () => setSource(tab.dataset.source));
  $(".lx-tabs-mini").addEventListener("keydown", (ev) => {
    if (!["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
    setSource(state.source === "file" ? "paste" : "file", true);
  });

  const input = $("#fileInput");
  input.addEventListener("change", () => setFile(input.files[0]));
  $("#fileChange").addEventListener("click", () => input.click());
  $("#fileRemove").addEventListener("click", () => { setFile(null); input.focus(); });
  const dz = $("#dropZone");
  ["dragenter", "dragover"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.remove("is-over"); }));
  dz.addEventListener("drop", (ev) => { if (ev.dataTransfer.files[0]) setFile(ev.dataTransfer.files[0]); });

  const ta = $("#contractText");
  let t = null;
  ta.addEventListener("input", () => {
    $("#charCount").textContent = `${ta.value.length.toLocaleString("tr-TR")} karakter`;
    fieldError("text", null);
    clearTimeout(t);
    t = setTimeout(() => suggestType(ta.value), 350);
  });

  for (const btn of $$("[data-sample]")) {
    btn.addEventListener("click", async () => {
      $("#sampleMenu").open = false;
      try {
        const { metin } = await api(`/api/samples/${btn.dataset.sample}`);
        setSource("paste");
        ta.value = metin;
        $("#charCount").textContent = `${metin.length.toLocaleString("tr-TR")} karakter`;
        const [type, role] = SAMPLE_PRESET[btn.dataset.sample];
        state.typeTouched = false;
        selectType(type, "Örnek sözleşme için seçildi.");
        selectRole(role);
        ta.focus({ preventScroll: true });
      } catch (e) { showFormError(e.message, false); }
    });
  }
  document.addEventListener("click", (ev) => {
    const menu = $("#sampleMenu");
    if (menu.open && !menu.contains(ev.target)) menu.open = false;
  });
}

/** Metne göre sözleşme türünü tahmin eder (sunucudaki algılamayla aynı kelimeler). */
function detectType(text) {
  const low = lowerTr(text);
  const words = {
    kira: /kiracı|kiraya veren|kiralanan|mecur|kira bedeli|kira sözleşmesi/g,
    hizmet: /hizmet|abone|üyelik|müşteri|paket|taahhüt|tüketici/g,
    is: /işçi|işveren|iş sözleşmesi|personel|çalışan|maaş|mesai|deneme süresi|kıdem/g,
  };
  let best = null, bestN = 0;
  for (const [t, rx] of Object.entries(words)) {
    const n = (low.match(rx) || []).length;
    if (n > bestN) { best = t; bestN = n; }
  }
  return bestN >= 3 ? best : null;
}

function suggestType(text) {
  if (state.typeTouched || text.trim().length < 80) return;
  const t = detectType(text);
  if (t && currentType() !== t) selectType(t, `Metne göre ${TYPE_NAMES[t].toLocaleLowerCase("tr")} olarak önerildi. Doğru değilse değiştirin.`);
}

/* ================================================================ tür ve rol */
const currentType = () => document.querySelector('input[name="ctype"]:checked')?.value || null;
const currentRole = () => document.querySelector('input[name="role"]:checked')?.value || null;

function selectType(type, hint = null) {
  const input = document.querySelector(`input[name="ctype"][value="${type}"]`);
  if (input) input.checked = true;
  renderRoles(type);
  $("#typeHint").hidden = !hint;
  $("#typeHint").textContent = hint || "";
  fieldError("type", null);
}

function selectRole(role) {
  const input = document.querySelector(`input[name="role"][value="${role}"]`);
  if (input) input.checked = true;
}

function renderRoles(type) {
  const box = $("#roleOptions");
  const prev = currentRole();
  box.replaceChildren();
  $("#roleField").hidden = !type;
  const isOther = type === "diger";
  $("#roleNoteWrap").hidden = !isOther;
  box.hidden = isOther;
  fieldError("role", null);
  if (!type || isOther) return;
  for (const [value, title, desc] of ROLE_OPTIONS[type]) {
    box.append(el("label", { class: "lx-role" },
      el("input", { type: "radio", name: "role", value, checked: value === prev ? true : null,
        onchange: () => fieldError("role", null) }),
      el("span", { class: "lx-radio", "aria-hidden": "true" }),
      el("span", {}, el("b", { text: title }), el("small", { text: desc }))));
  }
}

function bindTypeAndRole() {
  for (const r of $$('input[name="ctype"]')) {
    r.addEventListener("change", () => {
      state.typeTouched = true;
      selectType(r.value);
      if (r.value === "diger") $("#roleNote").focus();
    });
  }
  $("#roleNote").addEventListener("input", () => fieldError("role", null));
}

/* ================================================================ doğrulama ve hatalar */
function fieldError(name, msg) {
  const map = { file: ["#fileError", "#fileInput"], text: ["#textError", "#contractText"], type: ["#typeError", null], role: ["#roleError", "#roleNote"] };
  const [errSel, inputSel] = map[name];
  const box = $(errSel);
  box.hidden = !msg;
  box.textContent = msg || "";
  if (inputSel) {
    if (msg) $(inputSel).setAttribute("aria-invalid", "true");
    else $(inputSel).removeAttribute("aria-invalid");
  }
}
function clearFieldErrors() { ["file", "text", "type", "role"].forEach((n) => fieldError(n, null)); }

function showFormError(msg, retry = true) {
  $("#formError").hidden = !msg;
  $("#formErrorText").textContent = msg || "";
  $("#retryBtn").hidden = !retry;
}

/** Formu doğrular; geçerliyse istek tanımını döndürür. Hatalar ilgili alanın yanında gösterilir. */
function buildRequest() {
  clearFieldErrors();
  showFormError(null);
  let firstInvalid = null;
  const mark = (name, msg, focusEl) => { fieldError(name, msg); firstInvalid = firstInvalid || focusEl; };

  if (state.source === "file") {
    if (!state.file) mark("file", "Bir dosya seçin ya da “Metin yapıştır” sekmesini kullanın.", $("#fileInput"));
    else if (state.meta && state.file.size > state.meta.max_mb * 1024 * 1024) mark("file", `Dosya ${state.meta.max_mb} MB sınırını aşıyor.`, $("#fileInput"));
  } else {
    const len = $("#contractText").value.trim().length;
    if (!len) mark("text", "Sözleşme metnini yapıştırın.", $("#contractText"));
    else if (len < 40) mark("text", "Metin çok kısa. Sözleşmenin tamamını yapıştırın.", $("#contractText"));
  }
  const type = currentType();
  if (!type) mark("type", "Sözleşme türünü seçin.", document.querySelector('input[name="ctype"]'));
  const role = type === "diger" ? "taraf" : currentRole();
  const note = type === "diger" ? $("#roleNote").value.trim() : "";
  if (type && type !== "diger" && !role) mark("role", "Bu sözleşmedeki rolünüzü seçin.", document.querySelector('input[name="role"]'));
  if (type === "diger" && !note) mark("role", "Bu sözleşmedeki rolünüzü kısaca yazın.", $("#roleNote"));

  if (firstInvalid) { firstInvalid.focus(); return null; }
  return { source: state.source, file: state.file, text: $("#contractText").value.trim(),
           params: { sozlesme_turu: type, rol: role, mod: "otomatik", rol_aciklama: note } };
}

function sendRequest(req) {
  if (req.source === "file") {
    const fd = new FormData();
    fd.append("dosya", req.file);
    for (const [k, v] of Object.entries(req.params)) fd.append(k, v);
    return api("/api/analyze-file", { method: "POST", body: fd });
  }
  return api("/api/analyze", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ metin: req.text, ...req.params }),
  });
}

let busy = false;
async function submitAnalysis(reuse = false) {
  if (busy) return;
  const req = reuse ? state.lastRequest : buildRequest();
  if (!req) return;
  state.lastRequest = req;
  busy = true;
  const btn = $("#analyzeBtn");
  btn.disabled = true;
  btn.textContent = req.source === "file" ? "Dosya yükleniyor…" : "İnceleniyor…";
  $("#loadingTitle").textContent = req.source === "file" ? `“${req.file.name}” inceleniyor` : "Sözleşmeniz inceleniyor";
  showView("Loading");
  window.scrollTo({ top: 0 });
  try {
    const report = await sendRequest(req);
    state.report = report;
    state.chat = [];
    renderReport(report);
    showView("Results");
    window.scrollTo({ top: 0 });
    $("#verdictTitle").focus?.();
  } catch (err) {
    showView("Start");
    // Girilen metin, dosya ve seçimler korunur
    if (err.status === 422 || err.status === 413) {
      if (req.source === "file") fieldError("file", err.message); else fieldError("text", err.message);
    } else {
      showFormError(`İnceleme tamamlanamadı. ${err.message}`, true);
    }
    $("#incele").scrollIntoView();
  } finally {
    busy = false;
    btn.disabled = false;
    btn.textContent = "Sözleşmeyi incele";
  }
}

/* ================================================================ menü bağlantıları */
function bindNavigation() {
  for (const link of $$("[data-nav]")) {
    link.addEventListener("click", (ev) => {
      const target = document.querySelector(link.getAttribute("href"));
      if (!target) return;
      ev.preventDefault();
      if ($("#viewStart").hidden) showView("Start");
      requestAnimationFrame(() => target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }));
    });
  }
}

function bindCatchTabs() {
  const tabs = [...$$("#neleri [role=tab]")];
  const activate = (tab, focus) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", on);
      t.tabIndex = on ? 0 : -1;
      $(`#${t.getAttribute("aria-controls")}`).hidden = !on;
    }
    if (focus) tab.focus();
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => activate(t));
    t.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowRight") activate(tabs[(i + 1) % tabs.length], true);
      if (ev.key === "ArrowLeft") activate(tabs[(i + tabs.length - 1) % tabs.length], true);
    });
  });
}

/* ================================================================ sonuç */
function verdictFor(r) {
  const { yuksek, orta } = r.sayilar;
  const n = r.bulgular.length;
  if (r.mod !== "ai" && r.sozlesme_turu === "diger" && !n) {
    return { cls: "v-mid", text: "Bu sözleşme için yalnızca genel bir ön kontrol yapılabildi." };
  }
  if (!WEAK_ROLES.has(r.rol)) {
    return n ? { cls: "v-mid", text: "Bazı maddeler bir anlaşmazlıkta sizi korumayabilir." }
             : { cls: "v-low", text: "Yasaya aykırı görünen bir madde bulunamadı." };
  }
  if (yuksek >= 3 || r.risk_puani >= 70) return { cls: "v-high", text: "Bu hâliyle imzalamadan önce değişiklik isteyin." };
  if (yuksek >= 1 || orta >= 2) return { cls: "v-mid", text: "İmzalamadan önce dikkat etmeniz gereken maddeler var." };
  if (n) return { cls: "v-low", text: "Önemli bir sorun görünmüyor; birkaç noktayı kontrol edin." };
  return { cls: "v-low", text: "Dikkat gerektiren bir madde bulunamadı." };
}

function renderReport(r) {
  // Bağlam satırı
  const ctx = $("#contextLine");
  ctx.replaceChildren();
  if (r.dosya_adi) ctx.append(el("span", { text: r.dosya_adi }));
  ctx.append(el("span", { text: r.sozlesme_turu_ad }), el("span", { text: `Rolünüz: ${r.rol_ad.toLocaleLowerCase("tr")}` }));

  const v = verdictFor(r);
  $("#verdictTitle").className = `lx-verdict ${v.cls}`;
  $("#verdictTitle").setAttribute("tabindex", "-1");
  $("#verdictText").textContent = v.text;
  $("#summaryText").textContent = firstSentences(r.genel_ozet || "", 2);

  const dist = $("#distRow");
  dist.replaceChildren();
  for (const k of ["yuksek", "orta", "dusuk"]) {
    dist.append(el("li", {}, el("span", { class: `lx-sq lx-sq-${k}`, "aria-hidden": "true" }), el("b", { text: r.sayilar[k] }), `${RISK_SHORT[k]} risk`));
  }
  dist.append(el("li", {}, el("span", { class: "lx-sq lx-sq-eksik", "aria-hidden": "true" }), el("b", { text: r.eksik_maddeler.length }), "eksik konu"));
  $("#scoreNum").textContent = r.risk_puani;

  $("#negotiateBtn").hidden = !(r.bulgular.some((f) => f.risk !== "dusuk") || r.eksik_maddeler.length);

  $("#warningBox").hidden = !r.uyari;
  $("#warningText").textContent = r.uyari || "";
  $("#disclaimer").textContent = r.yasal_uyari;

  $("#cntReview").textContent = r.bulgular.length;
  $("#cntMissing").textContent = r.eksik_maddeler.length + r.sorulacak_sorular.length;
  $("#calcSection").hidden = r.sozlesme_turu !== "kira";

  resetFilters();
  renderFilterButtons(r);
  renderFindings(r);
  renderMissing(r);
  renderDates(r);
  renderChat();
  prefillCalculators(r);
  openPane("paneReview");
}

/* ---------- sekmeler ---------- */
function openPane(id, focus = false) {
  for (const tab of $$(".lx-tab")) {
    const on = tab.getAttribute("aria-controls") === id;
    tab.setAttribute("aria-selected", on);
    tab.tabIndex = on ? 0 : -1;
    if (on && focus) tab.focus();
  }
  for (const p of $$(".lx-pane")) p.hidden = p.id !== id;
}

/* ---------- filtreler ---------- */
function renderFilterButtons(r) {
  const box = $("#riskFilter");
  box.replaceChildren();
  const items = [["all", "Tümü", r.bulgular.length], ["yuksek", "Yüksek", r.sayilar.yuksek], ["orta", "Orta", r.sayilar.orta], ["dusuk", "Düşük", r.sayilar.dusuk]];
  for (const [key, label, n] of items) {
    box.append(el("button", { type: "button", class: "lx-seg-btn", "aria-pressed": key === state.filter ? "true" : "false",
      onclick: (ev) => { state.filter = key; for (const b of box.children) b.setAttribute("aria-pressed", b === ev.currentTarget); applyFilter(); } },
      label, el("span", { class: "lx-n", text: n })));
  }
}

function resetFilters() {
  state.filter = "all";
  for (const b of $$("#riskFilter .lx-seg-btn")) b.setAttribute("aria-pressed", b === $("#riskFilter").firstElementChild);
  $("#searchBox").value = "";
  $("#onlyInvalid").checked = false;
}

function applyFilter() {
  const q = lowerTr($("#searchBox").value.trim());
  const onlyInvalid = $("#onlyInvalid").checked;
  let visible = 0;
  for (const item of $$("#findings .lx-finding")) {
    const ok = (state.filter === "all" || item.dataset.risk === state.filter)
      && (!onlyInvalid || item.dataset.validity === "gecersiz_olabilir")
      && (!q || lowerTr(item.textContent).includes(q));
    item.hidden = !ok;
    if (ok) visible++;
  }
  const total = state.report ? state.report.bulgular.length : 0;
  $("#noFindings").hidden = !(total && !visible);
  $("#emptyFindings").hidden = total > 0;
}

/* ---------- bulgular ---------- */
function findingItem(f, idx) {
  const headId = `fh-${idx}`, bodyId = `fb-${idx}`;
  const effect = firstSentences(f.neden || f.sade || "", 1);

  const quoteBlock = f.alinti
    ? el("div", { class: "lx-block" },
        el("h4", { text: `Sözleşmedeki ifade${f.madde_baslik ? ` (${shortTitle(f.madde_baslik)})` : ""}` }),
        el("blockquote", { class: "lx-quote", text: f.alinti }),
        el("div", { class: "lx-inline-actions" },
          el("button", { type: "button", class: "lx-btn lx-btn-ghost lx-btn-sm", onclick: (ev) => openDoc(f, ev.currentTarget) },
            icon("bi-file-text"), "Sözleşmede göster")))
    : el("div", { class: "lx-block" },
        el("h4", { text: "Sözleşmedeki ifade" }),
        el("p", { class: "lx-quote-missing", text: "İlgili bölüm otomatik olarak eşleştirilemedi." }),
        el("button", { type: "button", class: "lx-btn lx-btn-ghost lx-btn-sm", onclick: (ev) => openDoc(f, ev.currentTarget) },
          icon("bi-file-text"), "Sözleşme metnini aç"));

  const alt = f.alternatif
    ? el("div", { class: "lx-block" },
        el("h4", { text: "Önerilen alternatif metin" }),
        el("p", { class: "lx-alt", text: f.alternatif }),
        el("button", { type: "button", class: "lx-btn lx-btn-ghost lx-btn-sm", onclick: () => copyText(f.alternatif, "Alternatif metin kopyalandı") },
          icon("bi-copy"), "Metni kopyala"))
    : null;

  const law = f.yasal_dayanak
    ? el("div", { class: "lx-law" }, el("b", { text: `${f.yasal_dayanak.kanun}, ${f.yasal_dayanak.madde}: ${f.yasal_dayanak.baslik}` }), f.yasal_dayanak.ozet)
    : el("p", { class: "lx-muted", text: "Bu bulgu için bilgi tabanımızda eşleşen bir kanun maddesi yok." });

  const source = el("details", { class: "lx-sub" },
    el("summary", {}, icon("bi-chevron-right"), "Hukuki dayanak ve kaynak"),
    el("div", { class: "lx-sub-body" },
      el("p", {}, el("b", { text: "Hukuki durum: " }), LEGAL_STATUS[f.gecerlilik] || LEGAL_STATUS.belirsiz),
      law,
      el("p", { class: "lx-muted", text: f.kaynak === "ai"
        ? "İnceleme yöntemi: Bu bulgu, sözleşmenin otomatik metin incelemesiyle tespit edildi."
        : "İnceleme yöntemi: Bu bulgu, bilinen riskli ifadeler listesiyle tespit edildi." })));

  const body = el("div", { class: "lx-finding-body", id: bodyId, role: "region", "aria-labelledby": headId, hidden: true },
    el("div", { class: "lx-block" }, el("h4", { text: "Bu madde ne diyor?" }), el("p", { text: f.sade })),
    el("div", { class: "lx-block" }, el("h4", { text: "Size etkisi" }), el("p", { text: f.neden })),
    el("div", { class: "lx-block" }, el("h4", { text: "Ne yapabilirsiniz?" }), el("p", { text: f.oneri })),
    quoteBlock, alt, source);

  const head = el("button", { type: "button", class: "lx-finding-head", id: headId, "aria-expanded": "false", "aria-controls": bodyId },
    el("span", { class: `lx-risk lx-risk-${f.risk}`, text: RISK_LABEL[f.risk] }),
    el("span", {}, el("span", { class: "lx-f-title", text: f.baslik }), el("span", { class: "lx-f-effect", text: effect })),
    icon("bi-chevron-down lx-f-chev"));
  head.addEventListener("click", () => toggleFinding(item));

  const item = el("li", { class: "lx-finding", dataset: { risk: f.risk, validity: f.gecerlilik, idx: String(idx) } },
    el("h3", {}, head), body);
  return item;
}

/** Aynı anda tek bulgu açık kalır; kapanan bulgu yüzünden kullanıcının konumu kaymaz. */
function toggleFinding(item, forceOpen = null) {
  const head = item.querySelector(".lx-finding-head");
  const willOpen = forceOpen ?? head.getAttribute("aria-expanded") !== "true";
  const before = head.getBoundingClientRect().top;
  for (const other of $$("#findings .lx-finding")) {
    if (other === item) continue;
    other.querySelector(".lx-finding-head").setAttribute("aria-expanded", "false");
    other.querySelector(".lx-finding-body").hidden = true;
  }
  head.setAttribute("aria-expanded", willOpen);
  item.querySelector(".lx-finding-body").hidden = !willOpen;
  const after = head.getBoundingClientRect().top;
  if (Math.abs(after - before) > 1) window.scrollBy(0, after - before);
}

function renderFindings(r) {
  const list = $("#findings");
  list.replaceChildren();
  r.bulgular.forEach((f, i) => list.append(findingItem(f, i)));
  const first = list.querySelector(".lx-finding");
  if (first) toggleFinding(first, true);
  applyFilter();
}

/* ---------- eksikler ve sorular ---------- */
function renderMissing(r) {
  const list = $("#missingList");
  list.replaceChildren();
  if (!r.eksik_maddeler.length) list.append(el("li", { class: "lx-muted", text: "Belirgin bir eksik bulunamadı." }));
  for (const m of r.eksik_maddeler) list.append(el("li", {}, el("b", { text: m.baslik }), el("span", { text: m.aciklama })));
  const ql = $("#questionList");
  ql.replaceChildren();
  for (const q of r.sorulacak_sorular) ql.append(el("li", { text: q }));
  if (!r.sorulacak_sorular.length) ql.append(el("li", { class: "lx-list-empty", text: "Bu sözleşme için önerilen soru yok." }));
  $("#copyQuestions").hidden = !r.sorulacak_sorular.length;
}

/* ---------- tarihler ---------- */
function renderDates(r) {
  const list = $("#dateList");
  list.replaceChildren();
  const dated = r.onemli_tarihler.filter((d) => d.tarih).sort((a, b) => a.tarih.localeCompare(b.tarih));
  const undated = r.onemli_tarihler.filter((d) => !d.tarih);
  if (!r.onemli_tarihler.length) list.append(el("li", {}, el("span", { class: "lx-muted", text: "Sözleşmede tarih bilgisi bulunamadı." })));
  for (const d of [...dated, ...undated]) {
    list.append(el("li", { class: d.hatirlatma ? "is-alarm" : "" },
      el("span", { class: "lx-t-date", text: d.tarih ? fmtDate(d.tarih) : "Süre" }),
      el("span", { class: "lx-t-desc" }, d.aciklama,
        d.kaynak === "hesap" ? el("span", { class: "lx-t-src", text: "Sözleşmedeki tarihlerden hesaplandı" }) : null)));
  }
  $("#icsBtn").hidden = !dated.length;
}

function downloadIcs() {
  const dates = state.report.onemli_tarihler.filter((d) => d.tarih);
  const esc = (s) => s.replace(/[\\,;]/g, (m) => "\\" + m).replace(/\n/g, "\\n");
  const stamp = new Date().toISOString().replace(/[-:]/g, "").split(".")[0] + "Z";
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//LexPlain//TR", "CALSCALE:GREGORIAN"];
  dates.forEach((d, i) => {
    const day = d.tarih.replace(/-/g, "");
    const next = new Date(d.tarih + "T00:00:00Z");
    next.setUTCDate(next.getUTCDate() + 1);
    lines.push("BEGIN:VEVENT", `UID:lexplain-${day}-${i}`, `DTSTAMP:${stamp}`,
      `DTSTART;VALUE=DATE:${day}`, `DTEND;VALUE=DATE:${next.toISOString().slice(0, 10).replace(/-/g, "")}`,
      `SUMMARY:${esc("Sözleşme: " + d.aciklama.split(":")[0].slice(0, 60))}`, `DESCRIPTION:${esc(d.aciklama)}`);
    if (d.hatirlatma) lines.push("BEGIN:VALARM", "TRIGGER:-P3D", "ACTION:DISPLAY", "DESCRIPTION:Sözleşme hatırlatması", "END:VALARM");
    lines.push("END:VEVENT");
  });
  lines.push("END:VCALENDAR");
  const a = el("a", { href: URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/calendar" })), download: "sozlesme-tarihleri.ics" });
  document.body.append(a);
  a.click();
  a.remove();
  toast("Takvim dosyası indirildi");
}

/* ---------- değişiklik talebi ---------- */
function buildNegotiationText() {
  const r = state.report;
  const items = r.bulgular.filter((f) => f.risk !== "dusuk");
  const lines = ["Merhaba,", "", "Sözleşme taslağını inceledim. İmzalamadan önce aşağıdaki maddelerin değiştirilmesini rica ediyorum.", ""];
  items.forEach((f, i) => {
    lines.push(`${i + 1}) ${f.madde_baslik ? shortTitle(f.madde_baslik) + ": " : ""}${f.baslik}`);
    if (f.yasal_dayanak && f.gecerlilik === "gecersiz_olabilir") lines.push(`   Not: ${f.yasal_dayanak.kanun} ${f.yasal_dayanak.madde} ile uyumlu olmayabilir.`);
    lines.push(f.alternatif ? `   Önerim: "${f.alternatif}"` : `   Önerim: ${f.oneri}`, "");
  });
  if (r.eksik_maddeler.length) {
    lines.push("Ayrıca aşağıdaki konuların sözleşmeye eklenmesini isterim:");
    r.eksik_maddeler.forEach((m) => lines.push(`- ${m.baslik}`));
    lines.push("");
  }
  lines.push("Bu değişiklikleri birlikte değerlendirebilirsek sevinirim.", "", "Saygılarımla");
  return lines.join("\n");
}

/* ---------- PDF ---------- */
function printReport() {
  toast("PDF hazırlanıyor. Açılan pencerede “PDF olarak kaydet”i seçin.");
  // Yazdırırken bütün bulgular ve kaynak ayrıntıları açık olsun
  const opened = [];
  for (const d of $$("#findings details:not([open])")) { d.open = true; opened.push(d); }
  const hiddenBodies = [...$$("#findings .lx-finding-body[hidden]")];
  hiddenBodies.forEach((b) => { b.hidden = false; });
  const restore = () => {
    opened.forEach((d) => { d.open = false; });
    hiddenBodies.forEach((b) => { b.hidden = true; });
    window.removeEventListener("afterprint", restore);
  };
  window.addEventListener("afterprint", restore);
  setTimeout(() => window.print(), 300);
}

/* ================================================================ belge paneli */
function bindDocPanel() {
  $("#docClose").addEventListener("click", closeDoc);
  $("#docBackdrop").addEventListener("click", closeDoc);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !$("#docPanel").hidden) closeDoc();
    if (ev.key === "Tab" && !$("#docPanel").hidden) {
      // odak panelin içinde kalsın
      const focusables = [$("#docClose"), $("#docText")];
      const i = focusables.indexOf(document.activeElement);
      if (ev.shiftKey && i <= 0) { ev.preventDefault(); focusables[focusables.length - 1].focus(); }
      else if (!ev.shiftKey && i === focusables.length - 1) { ev.preventDefault(); focusables[0].focus(); }
    }
  });
}

/** Sözleşme metnini açar; yalnızca doğrulanmış alıntı konumunu işaretler. */
function openDoc(f, trigger) {
  const r = state.report;
  const box = $("#docText");
  box.replaceChildren();
  const k = f.alinti_konum;
  const valid = k && k.start >= 0 && k.end <= r.metin.length && k.end > k.start && r.metin.slice(k.start, k.end) === f.alinti;
  let mark = null;
  if (valid) {
    mark = el("mark", { text: r.metin.slice(k.start, k.end) });
    box.append(r.metin.slice(0, k.start), mark, r.metin.slice(k.end));
  } else {
    box.append(r.metin);
  }
  $("#docNotice").hidden = !!valid;
  $("#docTitle").textContent = valid && f.madde_baslik ? shortTitle(f.madde_baslik) : "Sözleşme metni";
  state.docReturnFocus = trigger || document.activeElement;
  $("#docBackdrop").hidden = false;
  $("#docPanel").hidden = false;
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => {
    if (mark) box.scrollTop = Math.max(0, mark.offsetTop - box.offsetTop - box.clientHeight / 3);
    else box.scrollTop = 0;
    $("#docClose").focus();
  });
}

function closeDoc() {
  $("#docPanel").hidden = true;
  $("#docBackdrop").hidden = true;
  document.body.style.overflow = "";
  if (state.docReturnFocus) state.docReturnFocus.focus();
}

/* ================================================================ soru-cevap */
function renderChat() {
  const log = $("#chatLog");
  log.replaceChildren();
  for (const m of state.chat) log.append(chatBubble(m));
  log.hidden = !state.chat.length;
  log.scrollTop = log.scrollHeight;
  const sug = $("#chatSuggestions");
  sug.replaceChildren();
  if (state.chat.length) return;
  const examples = {
    kira: ["Kombi bozulursa kim öder?", "Erken çıkarsam ne kadar öderim?", "Depozitomu ne zaman geri alırım?"],
    hizmet: ["Aboneliği nasıl iptal ederim?", "Fiyat artarsa ne yapabilirim?", "Paramı geri alabilir miyim?"],
    is: ["İstifa edersem ne öderim?", "Fazla mesai ücretimi alabilir miyim?", "Deneme süresinde çıkarılabilir miyim?"],
    diger: ["En riskli madde hangisi?", "Sözleşmeden nasıl çıkabilirim?", "Ödeme yapmazsam ne olur?"],
  }[state.report.sozlesme_turu] || [];
  for (const q of examples) {
    sug.append(el("button", { type: "button", class: "lx-chip", text: q, onclick: () => { $("#chatInput").value = q; $("#chatForm").requestSubmit(); } }));
  }
}

function chatBubble(m) {
  const bubble = el("div", { class: `lx-msg lx-msg-${m.rol}` });
  if (m.pending) {
    bubble.append(el("span", { class: "lx-typing", "aria-label": "Cevap hazırlanıyor" }, el("i"), el("i"), el("i")));
    return bubble;
  }
  if (m.rol === "assistant") {
    for (const part of m.metin.split(/(\[m\d+\])/g)) {
      const match = part.match(/^\[(m\d+)\]$/);
      const clause = match && state.report.maddeler.find((c) => c.id === match[1]);
      if (clause) {
        bubble.append(el("button", { type: "button", class: "lx-cite", text: shortTitle(clause.title), title: "Bu maddeyi sözleşmede göster",
          onclick: (ev) => openDoc({ alinti: state.report.metin.slice(clause.start, clause.end), alinti_konum: { start: clause.start, end: clause.end }, madde_baslik: clause.title }, ev.currentTarget) }));
      } else {
        bubble.append(part);
      }
    }
  } else {
    bubble.textContent = m.metin;
  }
  return bubble;
}

async function sendQuestion(ev) {
  ev.preventDefault();
  const input = $("#chatInput");
  const soru = input.value.trim();
  if (soru.length < 2 || state.chat.some((m) => m.pending)) return;
  input.value = "";
  const history = state.chat.map(({ rol, metin }) => ({ rol, metin }));
  state.chat.push({ rol: "user", metin: soru });
  const pending = { rol: "assistant", metin: "", pending: true };
  state.chat.push(pending);
  renderChat();
  try {
    const r = state.report;
    const res = await api("/api/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ metin: r.metin, soru, sozlesme_turu: r.sozlesme_turu, rol: r.rol,
        rol_aciklama: r.rol === "taraf" ? r.rol_ad : "", gecmis: history }),
    });
    pending.metin = res.cevap;
    if (res.ilgili_maddeler?.length && !/\[m\d+\]/.test(res.cevap)) pending.metin += "\n" + res.ilgili_maddeler.map((id) => `[${id}]`).join(" ");
  } catch (err) {
    pending.metin = `Soru cevaplanamadı. ${err.message}`;
  }
  delete pending.pending;
  renderChat();
  input.focus();
}

/* ================================================================ hesaplamalar */
function prefillCalculators(r) {
  const m = r.metin.match(/aylık\s+kira(?:\s+bedeli)?\s*[:=]?\s*([\d.]+(?:,\d+)?)\s*(?:tl|₺|türk)/i);
  for (const id of ["#calcRent", "#depRent", "#exitRent"]) $(id).value = m ? parseFloat(m[1].replace(/\./g, "").replace(",", ".")) : "";
  updateCalculators();
}

const resultLine = (title, text, bad = false) => el("div", { class: `lx-result-line${bad ? " is-bad" : ""}` }, el("b", { text: title }), text ? el("span", { text }) : null);

function updateCalculators() {
  const rent = num("#calcRent"), tufe = num("#calcTufe"), pct = num("#calcContractPct");
  const o1 = $("#calcRentOut");
  o1.replaceChildren();
  if (rent > 0 && tufe >= 0) {
    const max = rent * (1 + tufe / 100);
    o1.append(resultLine(fmtTL(max), "Yasal olarak istenebilecek en yüksek yeni kira."));
    if (pct >= 0) {
      const asked = rent * (1 + pct / 100);
      o1.append(pct <= tufe
        ? resultLine("İstenen artış sınır içinde.", `Yeni kira: ${fmtTL(asked)}`)
        : resultLine(`${fmtTL(asked - max)} fazla isteniyor.`, `İstenen: ${fmtTL(asked)}. Sınırı aşan kısım geçersiz olabilir.`, true));
    }
  } else {
    o1.append(el("p", { class: "lx-help", text: "Kira ve TÜFE oranını girin." }));
  }
  const dr = num("#depRent"), da = num("#depAsked");
  const o2 = $("#depOut");
  o2.replaceChildren();
  if (dr > 0) {
    o2.append(resultLine(fmtTL(dr * 3), "İstenebilecek en yüksek depozito (3 aylık kira)."));
    if (da > 0) {
      const over = da - dr * 3;
      o2.append(over > 0 ? resultLine(`${fmtTL(over)} fazla isteniyor.`, "Konut kirasında depozito üç aylık kirayı aşamaz.", true)
                         : resultLine("İstenen depozito sınır içinde.", `${(da / dr).toLocaleString("tr-TR", { maximumFractionDigits: 1 })} aylık kira tutarında.`));
    }
  } else {
    o2.append(el("p", { class: "lx-help", text: "Aylık kirayı girin." }));
  }
  const er = num("#exitRent"), remain = num("#exitRemain"), relet = num("#exitRelet"), pen = num("#exitPenalty");
  const o3 = $("#exitOut");
  o3.replaceChildren();
  if (er > 0 && remain >= 0) {
    const months = Math.min(remain, Number.isNaN(relet) ? 2 : relet);
    o3.append(resultLine(fmtTL(months * er), `Tahmini ödemeniz: ev yeniden kiralanana kadar ${months} aylık kira.`));
    if (pen > 0) o3.append(resultLine(`Sözleşme ${fmtTL(pen * er)} istiyor.`, "Peşinen belirlenen sabit ceza geçersiz sayılabilir.", true));
  } else {
    o3.append(el("p", { class: "lx-help", text: "Kira ve kalan ay sayısını girin." }));
  }
}

function bindCalculators() {
  for (const i of $$("#calcSection input")) i.addEventListener("input", updateCalculators);
}

/* ================================================================ sonuç ekranı olayları */
function bindResults() {
  for (const tab of $$(".lx-tab")) tab.addEventListener("click", () => openPane(tab.getAttribute("aria-controls")));
  $(".lx-tabs").addEventListener("keydown", (ev) => {
    if (!["ArrowRight", "ArrowLeft"].includes(ev.key)) return;
    const tabs = [...$$(".lx-tab")];
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    const next = tabs[(i + (ev.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    openPane(next.getAttribute("aria-controls"), true);
  });
  $("#searchBox").addEventListener("input", applyFilter);
  $("#onlyInvalid").addEventListener("change", applyFilter);
  $("#advToggle").addEventListener("click", () => {
    const open = $("#advToggle").getAttribute("aria-expanded") !== "true";
    $("#advToggle").setAttribute("aria-expanded", open);
    $("#advFilters").hidden = !open;
  });
  $("#clearFilters").addEventListener("click", () => {
    resetFilters();
    renderFilterButtons(state.report);
    applyFilter();
  });
  $("#printBtn").addEventListener("click", printReport);
  $("#icsBtn").addEventListener("click", downloadIcs);
  $("#copyQuestions").addEventListener("click", () =>
    copyText(state.report.sorulacak_sorular.map((q, i) => `${i + 1}. ${q}`).join("\n"), "Sorular kopyalandı"));
  $("#negotiateBtn").addEventListener("click", () => {
    $("#negotiateText").value = buildNegotiationText();
    $("#negotiateDialog").showModal();
  });
  $("#copyNegotiate").addEventListener("click", () => copyText($("#negotiateText").value, "Değişiklik talebi kopyalandı"));
  $("#reanalyzeBtn").addEventListener("click", () => submitAnalysis(true));
  $("#newBtn").addEventListener("click", () => {
    showView("Start");
    $("#incele").scrollIntoView();
    $("#formTitle").setAttribute("tabindex", "-1");
    $("#formTitle").focus({ preventScroll: true });
  });
  $("#chatForm").addEventListener("submit", sendQuestion);
}

document.addEventListener("DOMContentLoaded", init);
