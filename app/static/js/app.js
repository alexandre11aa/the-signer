"use strict";

// Nothing is persisted: no localStorage, cookies or analytics.
// The PDF is parsed and rendered locally by pdf.js; it is only sent to the server on "Assinar".

// ---------- constants (mirror backend app/core/config.py and signer.py) ----------
const STAMP_SIZE = {            // PDF points
  default: { width: 210, height: 60 },
  adobe: { width: 208, height: 52 },
  custom: { width: 208, height: 52 },   // same layout as Adobe, different symbol
};
const STAMP_MARGIN = 20;
const MAX_PAGES_TO_SIGN = 100;
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const LOGO_PIXELS = 264;             // custom style image must be exactly 264x264 PNG

// Deliberately generic: the server keeps no logs, so the UI does not speculate about causes.
const MESSAGES = {
  signed: "PDF assinado.",
  invalidCertificate: "Certificado ou senha inválidos.",
  openPdf: "Não foi possível abrir o PDF.",
  logo: "Use uma imagem PNG de 264×264 px.",
  pages: "Verifique as páginas informadas.",
  generic: "Não foi possível concluir a operação. Verifique os dados e tente novamente.",
};

// ---------- elements ----------
const $ = (id) => document.getElementById(id);
const form = $("sign-form");
const pdfInput = $("pdf-file");
const pfxInput = $("pfx-file");
const logoInput = $("logo-file");
const logoField = $("logo-field");
const passwordInput = $("password");
const pagesInput = $("pages");
const toastRegion = $("toast-region");
const submitButton = $("submit-button");
const resultActions = $("result-actions");
const downloadButton = $("download-button");
const restartButton = $("restart-button");
const panelBody = document.querySelector(".panel-body");
const stage = $("stage");
const dropzone = $("dropzone");
const pageWrap = $("page-wrap");
const canvas = $("page-canvas");
const stamp = $("stamp");
const pager = $("pager");
const pageIndicator = $("page-indicator");
const pageState = $("page-state");
const prevButton = $("prev-page");
const nextButton = $("next-page");

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/js/vendor/pdf.worker.min.js";

// ---------- state ----------
let pdfDoc = null;
let totalPages = 0;
let currentPage = 1;
let renderToken = 0;
let activeRenderTask = null;
let signedBlob = null;        // signed PDF, kept in memory only until download/restart
let signedName = "";
let lastViewport = null;     // viewport at scale 1 of the page being shown (rotation included)

// ---------- helpers ----------
let toastTimer = 0;
/** One toast at a time. Click to dismiss; errors stay a bit longer. */
function showToast(message, kind = "success") {
  clearTimeout(toastTimer);
  const toast = document.createElement("div");
  toast.className = `toast ${kind}`;
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  toast.textContent = message;
  toast.addEventListener("click", () => toast.remove());
  toastRegion.replaceChildren(toast);
  toastTimer = setTimeout(() => toast.remove(), kind === "error" ? 6000 : 4000);
}

function clearToast() {
  clearTimeout(toastTimer);
  toastRegion.replaceChildren();
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",", 2)[1]);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function pagesText() {
  return form.elements["pages-mode"].value === "all" ? "all" : pagesInput.value;
}

/** Same rules as the backend parse_pages. Returns a Set of 1-based pages, or null if invalid. */
function parsePages(text, total) {
  text = text.trim().toLowerCase();
  if (text === "all" || text === "*") return new Set(Array.from({ length: total }, (_, i) => i + 1));
  const selected = new Set();
  for (const part of text.replace(/\s/g, "").split(",")) {
    if (!part) continue;
    const match = /^(\d+)(?:-(\d+))?$/.exec(part);
    if (!match) return null;
    const start = Number(match[1]);
    const end = match[2] ? Number(match[2]) : start;
    if (!(start >= 1 && start <= end && end <= total)) return null;
    for (let p = start; p <= end; p++) selected.add(p);
  }
  if (selected.size === 0 || selected.size > MAX_PAGES_TO_SIGN) return null;
  return selected;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
    credentials: "omit",
    referrerPolicy: "no-referrer",
  });
  if (!response.ok) {
    let code = "";
    try { code = (await response.json()).error; } catch { /* generic message */ }
    throw Object.assign(new Error("request_failed"), {
      userMessage: code === "invalid_certificate_or_password" ? MESSAGES.invalidCertificate
        : code === "invalid_logo" ? MESSAGES.logo : MESSAGES.generic,
    });
  }
  return response;
}

// ---------- position grid ----------
function buildPositionGrid() {
  const grid = $("position-grid");
  for (let i = 0; i < 9; i++) {
    const label = document.createElement("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "position";
    radio.value = String(i + 1);
    radio.checked = i + 1 === 9;
    radio.setAttribute("aria-label", `Posição ${i + 1}`);
    label.append(radio, document.createElement("span")); // the dot is drawn in CSS
    grid.append(label);
  }
}

// ---------- signature overlay ----------
function buildStampContent(style) {
  stamp.replaceChildren();
  const line = () => document.createElement("i");
  const column = (count, extra = "") => {
    const col = document.createElement("div");
    col.className = `col ${extra}`.trim();
    for (let n = 0; n < count; n++) col.append(line());
    return col;
  };
  if (style === "adobe" || style === "custom") stamp.append(column(2, "name"), column(3));
  else stamp.append(column(3));
}

function updateOverlay() {
  if (!pdfDoc || !lastViewport) { stamp.hidden = true; pageState.textContent = ""; return; }

  const selected = parsePages(pagesText(), totalPages);
  pageState.className = "page-state";
  if (!selected) {
    stamp.hidden = true;
    pageState.textContent = "Páginas inválidas";
    pageState.classList.add("bad");
    return;
  }
  if (!selected.has(currentPage)) {
    stamp.hidden = true;
    pageState.textContent = "Esta página não será assinada";
    pageState.classList.add("off");
    return;
  }
  pageState.textContent = "Esta página será assinada";
  pageState.classList.add("on");

  const style = form.elements.style.value;
  const position = Number(form.elements.position.value);
  const row = Math.floor((position - 1) / 3);
  const col = (position - 1) % 3;
  const { width, height } = STAMP_SIZE[style];
  const vw = lastViewport.width;     // visual page size in points (rotation already applied)
  const vh = lastViewport.height;
  const left = col === 0 ? STAMP_MARGIN : col === 1 ? (vw - width) / 2 : vw - width - STAMP_MARGIN;
  const top = row === 0 ? STAMP_MARGIN : row === 1 ? (vh - height) / 2 : vh - height - STAMP_MARGIN;

  const scale = canvas.clientWidth / vw;
  buildStampContent(style);
  stamp.style.left = `${left * scale}px`;
  stamp.style.top = `${top * scale}px`;
  stamp.style.width = `${width * scale}px`;
  stamp.style.height = `${height * scale}px`;
  stamp.hidden = false;
}

// ---------- PDF preview ----------
async function renderPage(number) {
  if (!pdfDoc) return;
  const token = ++renderToken;
  if (activeRenderTask) { try { activeRenderTask.cancel(); } catch { /* already done */ } }

  const page = await pdfDoc.getPage(number);
  if (token !== renderToken) return;

  const base = page.getViewport({ scale: 1 });
  lastViewport = base;

  // Fit to the stage width (and to its height on desktop), never upscale beyond 2x.
  const padding = 48;
  const maxWidth = Math.max(stage.clientWidth - padding, 160);
  const maxHeight = Math.max(stage.clientHeight - padding, 160);
  const fitWidth = maxWidth / base.width;
  const fitHeight = window.matchMedia("(max-width: 860px)").matches ? Infinity : maxHeight / base.height;
  const cssScale = Math.min(fitWidth, fitHeight, 2);

  const ratio = window.devicePixelRatio || 1;
  const viewport = page.getViewport({ scale: cssScale * ratio });
  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  canvas.style.width = `${Math.floor(viewport.width / ratio)}px`;
  canvas.style.height = `${Math.floor(viewport.height / ratio)}px`;

  activeRenderTask = page.render({ canvasContext: canvas.getContext("2d"), viewport });
  try {
    await activeRenderTask.promise;
  } catch (error) {
    if (error?.name === "RenderingCancelledException") return;
    throw error;
  }
  if (token !== renderToken) return;

  pageIndicator.textContent = `${number} / ${totalPages}`;
  prevButton.disabled = number <= 1;
  nextButton.disabled = number >= totalPages;
  updateOverlay();
}

function showEmptyPreview() {
  pdfDoc = null;
  totalPages = 0;
  lastViewport = null;
  pageWrap.hidden = true;
  pager.hidden = true;
  dropzone.hidden = false;
  stamp.hidden = true;
}

async function loadPdf(file) {
  $("pdf-name").textContent = file ? file.name : "Escolher PDF…";
  $("pdf-name").title = file ? file.name : "";
  $("pdf-name").parentElement.classList.toggle("has-file", Boolean(file));
  if (!file) { showEmptyPreview(); return; }

  try {
    const data = new Uint8Array(await file.arrayBuffer());
    if (pdfDoc) { pdfDoc.destroy(); }
    pdfDoc = await pdfjsLib.getDocument({ data, isEvalSupported: false }).promise;
    totalPages = pdfDoc.numPages;
    currentPage = 1;
    dropzone.hidden = true;
    pageWrap.hidden = false;
    pager.hidden = false;
    await renderPage(currentPage);
  } catch (error) {
    showEmptyPreview();
    pdfInput.value = "";
    $("pdf-name").textContent = "Escolher PDF…";
    $("pdf-name").parentElement.classList.remove("has-file");
    showToast(MESSAGES.openPdf, "error");
  }
}

// ---------- events ----------
pdfInput.addEventListener("change", () => loadPdf(pdfInput.files[0]));

pfxInput.addEventListener("change", () => {
  const file = pfxInput.files[0];
  $("pfx-name").textContent = file ? file.name : "Escolher certificado…";
  $("pfx-name").title = file ? file.name : "";
  $("pfx-name").parentElement.classList.toggle("has-file", Boolean(file));
});

prevButton.addEventListener("click", () => { if (currentPage > 1) renderPage(--currentPage); });
nextButton.addEventListener("click", () => { if (currentPage < totalPages) renderPage(++currentPage); });

$("toggle-password").addEventListener("click", (event) => {
  const show = passwordInput.type === "password";
  passwordInput.type = show ? "text" : "password";
  event.currentTarget.textContent = show ? "Ocultar" : "Mostrar";
  event.currentTarget.setAttribute("aria-pressed", String(show));
});

form.elements["pages-mode"].forEach((radio) => radio.addEventListener("change", () => {
  const custom = form.elements["pages-mode"].value === "custom";
  pagesInput.hidden = !custom;
  pagesInput.required = custom;
  if (custom) pagesInput.focus();
  updateOverlay();
}));
pagesInput.addEventListener("input", updateOverlay);
form.addEventListener("change", (event) => {
  if (event.target.name === "style") {
    const custom = form.elements.style.value === "custom";
    logoField.hidden = !custom;
    logoInput.required = custom;
  }
  if (event.target.name === "position" || event.target.name === "style") updateOverlay();
});

/** Accepts only a PNG of exactly 264x264 (the server checks again). */
async function isValidLogo(file) {
  if (!file) return false;
  // Check the PNG signature itself: the extension or MIME type alone can be wrong.
  const head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
  if (!PNG_SIGNATURE.every((byte, i) => head[i] === byte)) return false;
  let bitmap;
  try { bitmap = await createImageBitmap(file); } catch { return false; }
  const ok = bitmap.width === LOGO_PIXELS && bitmap.height === LOGO_PIXELS;
  bitmap.close();
  return ok;
}

logoInput.addEventListener("change", async () => {
  const file = logoInput.files[0];
  const valid = await isValidLogo(file);
  if (file && !valid) {
    logoInput.value = "";
    showToast(MESSAGES.logo, "error");
  }
  const kept = valid ? file : null;
  $("logo-name").textContent = kept ? kept.name : "Imagem PNG 264×264…";
  $("logo-name").title = kept ? kept.name : "";
  $("logo-name").parentElement.classList.toggle("has-file", Boolean(kept));
});

// Drag and drop onto the preview
["dragenter", "dragover"].forEach((name) => stage.addEventListener(name, (event) => {
  event.preventDefault();
  stage.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => stage.addEventListener(name, (event) => {
  event.preventDefault();
  stage.classList.remove("dragging");
}));
stage.addEventListener("drop", (event) => {
  const file = [...event.dataTransfer.files].find((f) => /\.pdf$/i.test(f.name) || f.type === "application/pdf");
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  pdfInput.files = transfer.files;
  loadPdf(file);
});

let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (pdfDoc) renderPage(currentPage); }, 150);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pdfDoc && !parsePages(pagesText(), totalPages)) {
    showToast(MESSAGES.pages, "error");
    return;
  }
  if (form.elements.style.value === "custom" && !logoInput.files[0]) {
    showToast(MESSAGES.logo, "error");
    return;
  }
  submitButton.disabled = true;
  submitButton.textContent = "Assinando…";
  submitButton.classList.add("loading");
  submitButton.setAttribute("aria-busy", "true");
  clearToast();
  try {
    const body = {
      pdf_b64: await fileToBase64(pdfInput.files[0]),
      pfx_b64: await fileToBase64(pfxInput.files[0]),
      password: passwordInput.value,
      pages: pagesText(),
      position: Number(form.elements.position.value),
      style: form.elements.style.value,
    };
    if (body.style === "custom") body.logo_b64 = await fileToBase64(logoInput.files[0]);
    const response = await postJson("/api/sign", body);
    signedBlob = await response.blob();
    signedName = pdfInput.files[0].name.replace(/\.pdf$/i, "") + "_signed.pdf";
    showResult();
  } catch (error) {
    showToast(error.userMessage || MESSAGES.generic, "error");
  } finally {
    passwordInput.value = ""; // do not keep the password in the DOM
    submitButton.textContent = "Assinar";
    submitButton.classList.remove("loading");
    submitButton.removeAttribute("aria-busy");
    submitButton.disabled = false;
  }
});

// ---------- result state: download or start over ----------
function showResult() {
  panelBody.inert = true;               // lock inputs: the result would no longer match them
  submitButton.hidden = true;
  resultActions.hidden = false;
  showToast(MESSAGES.signed, "success");
  downloadButton.focus();
}

downloadButton.addEventListener("click", () => {
  if (!signedBlob) return;
  const url = URL.createObjectURL(signedBlob);
  const link = document.createElement("a");
  link.href = url;
  link.download = signedName;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
});

restartButton.addEventListener("click", () => {
  signedBlob = null;                    // drop the signed PDF from memory
  signedName = "";
  pdfInput.value = "";                  // new PDF; the certificate stays selected
  loadPdf(null);
  panelBody.inert = false;
  resultActions.hidden = true;
  submitButton.hidden = false;
  clearToast();
  passwordInput.focus();
});

buildPositionGrid();
