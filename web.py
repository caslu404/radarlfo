import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, render_template_string

# ==========================================================
# FLAGS
# ==========================================================
DEBUG_SCRAPE = False
VERBOSE_LOG = False

# ==========================================================
# CONSTANTES
# ==========================================================
AMAZON_DOMAIN = "https://www.amazon.com.br"

AMAZON_RETAIL_HINTS = [
    "amazon",
    "amazon.com.br",
    "amazon serviços de varejo",
    "amazon servicos de varejo",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

MAX_WORKERS = 6
REQUEST_TIMEOUT = 10.0

# Retries "dentro" do safe_get
SAFEGET_RETRIES_PER_CYCLE = 2

# "Ciclos" quando detectar bloqueio: renova sessão e tenta de novo
MAX_BLOCKED_CYCLES = 3
BLOCKED_BACKOFF_BASE = 1.8

REPROCESSAR_LABEL = "Reprocessar"

# ==========================================================
# FLASK
# ==========================================================
app = Flask(__name__)
app.secret_key = "lfo_web_scrape_only_2026"

# ==========================================================
# HELPERS
# ==========================================================
_thread_local = threading.local()


def is_valid_asin(a: str) -> bool:
    a = (a or "").strip()
    return len(a) == 10 and a.isalnum()


def normalize_spaces(s: str) -> str:
    return " ".join((s or "").split()).strip()


def is_amazon_name(name: str) -> bool:
    n = (name or "").lower().strip()
    if not n:
        return False
    return any(h in n for h in AMAZON_RETAIL_HINTS)


def detect_soft_block(resp: requests.Response, html: str) -> bool:
    if resp is None:
        return True
    if resp.status_code in (429, 503):
        return True

    t = (html or "").lower()
    if "digite os caracteres que você vê" in t:
        return True
    if "to discuss automated access" in t:
        return True
    if "sorry" in t and "robot" in t:
        return True
    return False


def get_session(force_new: bool = False) -> requests.Session:
    """
    Uma Session por thread. Se force_new=True, recria a session (cookies novos).
    """
    if force_new:
        _thread_local.session = None

    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.session = s
    return s


def safe_get(session: requests.Session, url: str, timeout: float, max_retries: int):
    last = (None, "")
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            html = resp.text if resp is not None else ""

            if not detect_soft_block(resp, html):
                return resp, html

            last = (resp, html)
            if DEBUG_SCRAPE:
                print(
                    f"[HTTP] Possível bloqueio em {url} "
                    f"status={getattr(resp,'status_code',None)} attempt={attempt}"
                )

        except Exception as e:
            last = (None, "")
            if DEBUG_SCRAPE:
                print(f"[HTTP] Erro GET {url}: {e}")

        time.sleep(0.7 + attempt * 1.1 + random.random() * 0.6)

    return last


def extract_sold_shipped_from_dp(soup: BeautifulSoup):
    """
    2026: extrai Vendido por / Enviado por do bloco ODF.
    Corrige:
      - Amazon sem <a> (usa <span>)
      - "Enviado / Vendido" => enviado = vendido (ATTO, Motorola, etc)
      - default de envio não pode ser Amazon; deve ser MFN (enviado = vendido)
    """

    def clean(s: str) -> str:
        s = normalize_spaces(s or "")
        if not s:
            return "Sem Oferta"
        s = re.sub(r"\s+", " ", s).strip()
        return s[:80] if s else "Sem Oferta"

    def pick_merchant_text(root) -> str:
        if not root:
            return "Sem Oferta"

        a = root.select_one(
            'a#sellerProfileTriggerId, a[href*="/gp/help/seller/"], a.offer-display-feature-text-message'
        )
        if a:
            return clean(a.get_text(" ", strip=True))

        sp = root.select_one("span.offer-display-feature-text-message")
        if sp:
            return clean(sp.get_text(" ", strip=True))

        return clean(root.get_text(" ", strip=True))

    vendido = "Sem Oferta"
    enviado = "Sem Oferta"

    label_el = soup.select_one(
        'div.offer-display-feature-label[offer-display-feature-name="desktop-merchant-info"]'
    )
    text_el = soup.select_one(
        'div.offer-display-feature-text[offer-display-feature-name="desktop-merchant-info"]'
    )

    label_txt = clean(label_el.get_text(" ", strip=True)).lower() if label_el else ""
    merchant_txt = pick_merchant_text(text_el)

    if merchant_txt != "Sem Oferta":
        if is_amazon_name(merchant_txt):
            return "Amazon", "Amazon"

        vendido = merchant_txt

        if "enviado / vendido" in label_txt or "shipped from and sold by" in label_txt:
            enviado = vendido
        else:
            enviado = "Sem Oferta"

    if vendido == "Sem Oferta":
        seller_links = soup.select(
            "#sellerProfileTriggerId, a[href*='/gp/help/seller/'], .sellerName a, [id*='seller'] a"
        )
        for link in seller_links:
            name = clean(link.get_text(" ", strip=True))
            if name != "Sem Oferta" and len(name.split()) <= 6:
                if is_amazon_name(name):
                    return "Amazon", "Amazon"
                vendido = name
                break

    if vendido != "Sem Oferta" and enviado == "Sem Oferta":
        page_text = soup.get_text(" ", strip=True).lower()

        if "enviado por amazon" in page_text or "ships from amazon" in page_text:
            enviado = "Amazon"
        else:
            prime_hint = soup.select_one(
                ".a-icon-prime, [alt*='Prime'], #primeBadge, .prime-badge"
            )
            enviado = "Amazon" if prime_hint else vendido

    if vendido == "Sem Oferta" and enviado == "Sem Oferta":
        return "Sem Oferta", "Sem Oferta"

    return vendido, enviado


def scrape_one_asin(asin: str):
    url = f"{AMAZON_DOMAIN}/dp/{asin}?language=pt_BR"

    # Ciclos: se bloqueou, espera, renova sessão e tenta de novo
    for cycle in range(MAX_BLOCKED_CYCLES + 1):
        session = get_session(force_new=(cycle > 0))

        resp, html = safe_get(
            session=session,
            url=url,
            timeout=REQUEST_TIMEOUT,
            max_retries=SAFEGET_RETRIES_PER_CYCLE,
        )

        # Sem resposta / vazio -> tenta próximo ciclo
        if resp is None or not html:
            if cycle < MAX_BLOCKED_CYCLES:
                time.sleep((BLOCKED_BACKOFF_BASE ** cycle) + random.random() * 0.8)
                continue
            return asin, REPROCESSAR_LABEL, REPROCESSAR_LABEL, "REPROCESSAR"

        # Bloqueio detectado -> tenta próximo ciclo (com backoff maior e sessão nova)
        if detect_soft_block(resp, html):
            if DEBUG_SCRAPE:
                print(
                    f"[BLOCKED] asin={asin} cycle={cycle} status={getattr(resp,'status_code',None)}"
                )
            if cycle < MAX_BLOCKED_CYCLES:
                time.sleep(
                    (BLOCKED_BACKOFF_BASE ** (cycle + 1)) + random.random() * 1.2
                )
                continue
            return asin, REPROCESSAR_LABEL, REPROCESSAR_LABEL, "REPROCESSAR"

        # Status code != 200
        if resp.status_code != 200:
            return asin, "Sem Oferta", "Sem Oferta", "Sem Oferta"

        # Parse OK
        soup = BeautifulSoup(html, "html.parser")
        vendido, enviado = extract_sold_shipped_from_dp(soup)

        if vendido == "Sem Oferta" and enviado == "Sem Oferta":
            status = "Sem Oferta"
        else:
            fo_amazon = is_amazon_name(vendido) and is_amazon_name(enviado)
            status = "Amazon FO" if fo_amazon else "LFO"

        time.sleep(0.06 + random.random() * 0.10)
        return asin, vendido, enviado, status

    return asin, REPROCESSAR_LABEL, REPROCESSAR_LABEL, "REPROCESSAR"


def get_offer_info_scrape_parallel(asins):
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(scrape_one_asin, asin) for asin in asins]
        for fut in as_completed(futures):
            asin, vendido, enviado, status = fut.result()
            results[asin] = (vendido, enviado, status)
    return results


def format_tempo_leitura(seconds: float) -> str:
    try:
        s = int(round(float(seconds)))
    except Exception:
        return "0seg"

    if s < 60:
        return f"{s}seg"
    m = s // 60
    r = s % 60
    return f"{m}min {r}seg"


# ==========================================================
# HTML
# ==========================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Radar LFO</title>

  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }

    body {
      font-family: Arial, sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      min-height: 100vh;
      padding: 20px;
    }

    .page {
      width: min(1600px, 96vw);
      margin: 0 auto;
      background: #ffffff;
      border-radius: 12px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.22);
      padding: 32px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }

    h1 {
      color: #232f3e;
      font-size: 30px;
      display: flex;
      align-items: center;
      gap: 10px;
      line-height: 1.2;
    }

    .subtitle {
      color: #666;
      font-size: 13px;
      margin-top: 6px;
    }

    .layout {
      display: grid;
      grid-template-columns: 380px 1fr;
      gap: 22px;
      align-items: start;
    }

    .card {
      background: #ffffff;
      border-radius: 12px;
      box-shadow: 0 8px 20px rgba(0,0,0,0.08);
      padding: 18px;
      border: 1px solid rgba(0,0,0,0.06);
    }

    label {
      display: block;
      font-weight: 700;
      margin-bottom: 10px;
      color: #232f3e;
      font-size: 14px;
    }

    textarea {
      width: 100%;
      min-height: 240px;
      padding: 14px;
      border: 2px solid #e2e2e2;
      border-radius: 10px;
      font-size: 13px;
      font-family: "Courier New", monospace;
      resize: vertical;
      transition: border-color 0.2s ease;
      outline: none;
    }

    textarea:focus { border-color: #ff9900; }

    .btn-row {
      display: flex;
      gap: 10px;
      margin-top: 12px;
    }

    button {
      padding: 12px 14px;
      font-size: 14px;
      font-weight: 700;
      border: none;
      border-radius: 10px;
      cursor: pointer;
      transition: all 0.2s ease;
      user-select: none;
    }

    .btn-primary {
      background: #ff9900;
      color: #ffffff;
      flex: 1;
    }

    .btn-primary:hover {
      background: #e88b00;
      transform: translateY(-1px);
      box-shadow: 0 6px 16px rgba(255, 153, 0, 0.28);
    }

    .btn-secondary {
      background: #f0f0f0;
      color: #232f3e;
      border: 2px solid transparent;
    }

    .btn-secondary:hover { background: #e6e6e6; }

    .btn-secondary.copied {
      background: #e8f5e9;
      color: #2e7d32;
      border-color: #2e7d32;
    }

    .btn-secondary.active {
      background: #ff9900;
      color: #ffffff;
      border-color: #ff9900;
    }

    .right-top {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      margin-bottom: 12px;
    }

    .stats {
      display: grid;
      grid-template-columns: repeat(6, minmax(170px, 1fr));
      gap: 12px;
    }

    .stat {
      background: #f8f9fa;
      border-radius: 10px;
      padding: 14px;
      border-left: 4px solid #ff9900;
      min-height: 78px;
    }

    .stat-label {
      font-size: 12px;
      color: #666;
      margin-bottom: 6px;
    }

    .stat-value {
      font-size: 20px;
      font-weight: 800;
      color: #232f3e;
    }
    .stat-sub {
      font-size: 12px;
      font-weight: 700;
      color: #666;
      margin-left: 8px;
      white-space: nowrap;
    }

    .stat-value.lfo { color: #d32f2f; }
    .stat-value.nooffer { color: #b7791f; }
    .stat-value.ok  { color: #388e3c; }
    .stat-value.reproc { color: #1565c0; }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 10px;
    }

    .toolbar-left, .toolbar-right {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      background: #ffffff;
      border-radius: 10px;
      overflow: hidden;
    }

    th {
      background: #232f3e;
      color: #ffffff;
      padding: 12px;
      text-align: left;
      font-weight: 800;
      font-size: 13px;
    }

    td {
      padding: 12px;
      border-bottom: 1px solid #e7e7e7;
      font-size: 13px;
    }

    tr:hover { background: #f8f9fa; }

    tr.lfo-row { background: #ffebee; }
    tr.lfo-row:hover { background: #ffd6d6; }

    tr.nooffer-row { background: #fff8e1; }
    tr.nooffer-row:hover { background: #ffefbf; }

    tr.reproc-row { background: #e3f2fd; }
    tr.reproc-row:hover { background: #cfe8ff; }

    .status-lfo { color: #d32f2f; font-weight: 800; }
    .status-ok  { color: #388e3c; font-weight: 800; }
    .status-nooffer { color: #b7791f; font-weight: 800; }
    .status-reproc { color: #1565c0; font-weight: 800; }

    .muted { color: #777; font-size: 13px; }

    #loading-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.25);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 9999;
      padding: 18px;
    }

    .loader-box {
      background: #ffffff;
      padding: 22px 26px;
      border-radius: 12px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.25);
      font-size: 15px;
      text-align: center;
      width: min(420px, 92vw);
    }

    .spinner {
      border: 4px solid #f3f3f3;
      border-top: 4px solid #ff9900;
      border-radius: 50%;
      width: 36px;
      height: 36px;
      margin: 0 auto 10px auto;
      animation: spin 0.9s linear infinite;
    }

    @keyframes spin { 0% { transform: rotate(0deg);} 100% { transform: rotate(360deg);} }

    @media (max-width: 1300px) {
      .stats { grid-template-columns: repeat(3, minmax(170px, 1fr)); }
    }

    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
      textarea { min-height: 190px; }
      .stats { grid-template-columns: repeat(2, minmax(170px, 1fr)); }
    }

    @media (max-width: 520px) {
      .page { padding: 18px; }
      .stats { grid-template-columns: 1fr; }
      button { width: 100%; }
      .btn-row { flex-direction: column; }
      .toolbar { flex-direction: column; align-items: stretch; }
      .toolbar-left, .toolbar-right { width: 100%; }
    }
  </style>

  <script>
    let filtroLfo = false;
    let filtroSemOferta = false;
    let filtroAmazonFo = false;
    let filtroReprocessar = false;

    let timerCopyTabela = null;
    let timerCopyAsins = null;

    document.addEventListener("DOMContentLoaded", function() {
      const form = document.getElementById("asin-form");
      const overlay = document.getElementById("loading-overlay");
      const loadingText = document.getElementById("loading-text");

      if (form && overlay && loadingText) {
        form.addEventListener("submit", function() {
          loadingText.textContent = "Consultando ASINs";
          overlay.style.display = "flex";
        });
      }

      const textarea = document.querySelector("textarea[name='asins']");
      if (textarea) textarea.focus();

      aplicaFiltrosTabela();
    });

    function toggleFiltro(tipo, btn) {
      if (tipo === "lfo") filtroLfo = !filtroLfo;
      if (tipo === "nooffer") filtroSemOferta = !filtroSemOferta;
      if (tipo === "amazonfo") filtroAmazonFo = !filtroAmazonFo;
      if (tipo === "reprocessar") filtroReprocessar = !filtroReprocessar;

      if (btn) {
        const isActive =
          (tipo === "lfo" && filtroLfo) ||
          (tipo === "nooffer" && filtroSemOferta) ||
          (tipo === "amazonfo" && filtroAmazonFo) ||
          (tipo === "reprocessar" && filtroReprocessar);

        btn.classList.toggle("active", isActive);
      }

      aplicaFiltrosTabela();
      resetCopiarTabela();
      resetCopiarAsins();
    }

    function aplicaFiltrosTabela() {
      const rows = document.querySelectorAll("#tabelaResultados tbody tr");
      const algumFiltroAtivo = filtroLfo || filtroSemOferta || filtroAmazonFo || filtroReprocessar;

      rows.forEach(row => {
        const status = ((row.dataset.status || "").toLowerCase()).trim();

        if (!algumFiltroAtivo) {
          row.style.display = "";
          return;
        }

        const ehLfo = (status === "lfo");
        const ehSemOferta = (status === "sem oferta");
        const ehAmazonFo = (status === "amazon fo");
        const ehReprocessar = (status === "reprocessar");

        const passa =
          (filtroLfo && ehLfo) ||
          (filtroSemOferta && ehSemOferta) ||
          (filtroAmazonFo && ehAmazonFo) ||
          (filtroReprocessar && ehReprocessar);

        row.style.display = passa ? "" : "none";
      });
    }

    function setBotaoCopiado(btn, timerVarName, resetFn) {
      if (!btn) return;

      btn.classList.add("copied");
      btn.textContent = "Copiado";

      if (window[timerVarName]) clearTimeout(window[timerVarName]);
      window[timerVarName] = setTimeout(() => {
        resetFn();
      }, 3000);
    }

    function resetCopiarTabela() {
      const btn = document.getElementById("copyBtn");
      if (!btn) return;
      btn.classList.remove("copied");
      btn.textContent = "Copiar tabela";
      if (timerCopyTabela) {
        clearTimeout(timerCopyTabela);
        timerCopyTabela = null;
      }
    }

    function resetCopiarAsins() {
      const btn = document.getElementById("copyAsinsBtn");
      if (!btn) return;
      btn.classList.remove("copied");
      btn.textContent = "Copiar ASINs";
      if (timerCopyAsins) {
        clearTimeout(timerCopyAsins);
        timerCopyAsins = null;
      }
    }

    function copiarTabela() {
      const tabela = document.getElementById("tabelaResultados");
      const botao  = document.getElementById("copyBtn");
      if (!tabela) return;

      let texto = "";
      const headRow = tabela.querySelector("thead tr");
      if (headRow) {
        const cols = Array.from(headRow.cells).map(c => (c.innerText || "").trim());
        texto += cols.join("\\t") + "\\n";
      }

      const bodyRows = tabela.querySelectorAll("tbody tr");
      bodyRows.forEach(row => {
        if (row.style.display === "none") return;
        const cols = Array.from(row.cells).map(c => (c.innerText || "").trim().replace(/\\t/g, " "));
        texto += cols.join("\\t") + "\\n";
      });

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(texto).then(() => setBotaoCopiado(botao, "timerCopyTabela", resetCopiarTabela));
      } else {
        const ta = document.createElement("textarea");
        ta.value = texto;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); setBotaoCopiado(botao, "timerCopyTabela", resetCopiarTabela); } catch(e) {}
        document.body.removeChild(ta);
      }
    }

    function copiarAsins() {
      const tabela = document.getElementById("tabelaResultados");
      const botao  = document.getElementById("copyAsinsBtn");
      if (!tabela) return;

      let texto = "";
      const bodyRows = tabela.querySelectorAll("tbody tr");
      bodyRows.forEach(row => {
        if (row.style.display === "none") return;
        const asin = row.cells && row.cells[0] ? row.cells[0].innerText.trim() : "";
        if (asin) texto += asin + "\\n";
      });

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(texto).then(() => setBotaoCopiado(botao, "timerCopyAsins", resetCopiarAsins));
      } else {
        const ta = document.createElement("textarea");
        ta.value = texto;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); setBotaoCopiado(botao, "timerCopyAsins", resetCopiarAsins); } catch(e) {}
        document.body.removeChild(ta);
      }
    }

    function limparTudo() {
      const textarea = document.querySelector("textarea[name='asins']");
      if (textarea) textarea.value = "";

      filtroLfo = false;
      filtroSemOferta = false;
      filtroAmazonFo = false;
      filtroReprocessar = false;

      const btnLfo = document.getElementById("filterLfoBtn");
      const btnNoOffer = document.getElementById("filterNoOfferBtn");
      const btnAmazonFo = document.getElementById("filterAmazonFoBtn");
      const btnReprocessar = document.getElementById("filterReprocessarBtn");
      if (btnLfo) btnLfo.classList.remove("active");
      if (btnNoOffer) btnNoOffer.classList.remove("active");
      if (btnAmazonFo) btnAmazonFo.classList.remove("active");
      if (btnReprocessar) btnReprocessar.classList.remove("active");

      resetCopiarTabela();
      resetCopiarAsins();

      aplicaFiltrosTabela();
    }
  </script>
</head>

<body>
  <div id="loading-overlay">
    <div class="loader-box">
      <div class="spinner"></div>
      <div id="loading-text">Consultando ASINs</div>
    </div>
  </div>

  <div class="page">
    <header>
      <div>
        <h1>📊 Radar LFO</h1>
        <div class="subtitle">What's in the [Buy] Box?</div>
      </div>
    </header>

    <div class="layout">
      <div class="card">
        <form method="POST" id="asin-form">
          <label>ASINs</label>
          <textarea
            name="asins"
            placeholder="Cole ASINs por linha, vírgula ou espaço"
          >{{ asin_text }}</textarea>

          <div class="btn-row">
            <button class="btn-primary" type="submit">Buscar LFO</button>
            <button class="btn-secondary" type="button" onclick="limparTudo()">Limpar</button>
          </div>
        </form>
      </div>

      <div class="card">
        {% if results %}
          <div class="right-top">
            <div class="stats">
              <div class="stat">
                <div class="stat-label">Total de ASINs</div>
                <div class="stat-value">{{ summary.total }}</div>
              </div>

              <div class="stat">
                <div class="stat-label">ASINs em LFO</div>
                <div class="stat-value lfo">{{ summary.lfo_count }}</div>
              </div>

              <div class="stat">
                <div class="stat-label">ASINs Sem Oferta</div>
                <div class="stat-value nooffer">{{ summary.nooffer_count }}</div>
              </div>

              <div class="stat">
                <div class="stat-label">ASINs Reprocessar</div>
                <div class="stat-value reproc">{{ summary.reproc_count }}</div>
              </div>

              <div class="stat">
                <div class="stat-label">LFO (%)</div>
                <div class="stat-value lfo">{{ "%.1f"|format(summary.lfo_pct) }}%</div>
              </div>

              <div class="stat">
                <div class="stat-label">Tempo de leitura</div>
                <div class="stat-value ok">
                  {{ read_time }}
                  <span class="stat-sub">
                    (
                    {% if sec_per_asin > 0 %}
                      {{ "%.2f"|format(sec_per_asin) }}
                    {% else %}
                      0.00
                    {% endif %}
                    /ASIN)
                  </span>
                </div>
              </div>
            </div>

            <div class="toolbar">
              <div class="toolbar-left">
                <button
                  class="btn-secondary"
                  id="filterAmazonFoBtn"
                  type="button"
                  onclick="toggleFiltro('amazonfo', this)"
                >
                  Amazon FO
                </button>

                <button
                  class="btn-secondary"
                  id="filterLfoBtn"
                  type="button"
                  onclick="toggleFiltro('lfo', this)"
                >
                  LFO
                </button>

                <button
                  class="btn-secondary"
                  id="filterNoOfferBtn"
                  type="button"
                  onclick="toggleFiltro('nooffer', this)"
                >
                  Sem Oferta
                </button>

                <button
                  class="btn-secondary"
                  id="filterReprocessarBtn"
                  type="button"
                  onclick="toggleFiltro('reprocessar', this)"
                >
                  Reprocessar
                </button>
              </div>

              <div class="toolbar-right">
                <button id="copyBtn" class="btn-secondary" type="button" onclick="copiarTabela()">Copiar tabela</button>
                <button id="copyAsinsBtn" class="btn-secondary" type="button" onclick="copiarAsins()">Copiar ASINs</button>
              </div>
            </div>
          </div>

          <table id="tabelaResultados">
            <thead>
              <tr>
                <th>ASIN</th>
                <th>Vendido por</th>
                <th>Enviado por</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {% for r in results %}
                <tr
                  data-status="{{ r.status }}"
                  class="{% if r.status == 'LFO' %}lfo-row{% elif r.status == 'Sem Oferta' %}nooffer-row{% elif r.status == 'Reprocessar' %}reproc-row{% endif %}"
                >
                  <td><strong>{{ r.asin }}</strong></td>
                  <td>{{ r.vendido }}</td>
                  <td>{{ r.enviado }}</td>
                  <td class="{% if r.status == 'LFO' %}status-lfo{% elif r.status == 'Sem Oferta' %}status-nooffer{% elif r.status == 'Reprocessar' %}status-reproc{% else %}status-ok{% endif %}">
                    {{ r.status }}
                  </td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        {% else %}
          <div class="muted">
            Nenhum resultado ainda. Cole os ASINs e clique em Buscar LFO.
          </div>
        {% endif %}
      </div>
    </div>
  </div>
</body>
</html>
"""

# ==========================================================
# ROTAS
# ==========================================================
@app.route("/", methods=["GET", "POST"])
def index():
    asin_text = ""
    results_display = []
    elapsed = 0.0
    sec_per_asin = 0.0
    read_time = "0seg"

    summary = {
        "total": 0,
        "lfo_count": 0,
        "nooffer_count": 0,
        "reproc_count": 0,
        "lfo_pct": 0.0,
    }

    if request.method == "POST":
        asin_text = request.form.get("asins", "")

        raw_text = asin_text.replace(",", " ")
        tokens = re.split(r"\s+", raw_text)
        asins_raw = [a.strip().upper() for a in tokens if a.strip()]
        valid_asins = [a for a in asins_raw if is_valid_asin(a)]

        if valid_asins:
            start = time.perf_counter()
            result_dict = get_offer_info_scrape_parallel(valid_asins)
            elapsed = time.perf_counter() - start

            read_time = format_tempo_leitura(elapsed)
            sec_per_asin = (elapsed / len(valid_asins)) if valid_asins else 0.0

            full_results = []
            for asin in valid_asins:
                vendido, enviado, status = result_dict.get(
                    asin, ("Sem Oferta", "Sem Oferta", "Sem Oferta")
                )

                # NORMALIZA status para a UI
                if status == "REPROCESSAR":
                    status_ui = "Reprocessar"
                else:
                    status_ui = status

                full_results.append(
                    {
                        "asin": asin,
                        "vendido": vendido,
                        "enviado": enviado,
                        "status": status_ui,
                    }
                )

            total = len(full_results)
            lfo_count = sum(1 for r in full_results if r["status"] == "LFO")
            amazonfo_count = sum(1 for r in full_results if r["status"] == "Amazon FO")
            reproc_count = sum(1 for r in full_results if r["status"] == "Reprocessar")

            # "Sem Oferta" real: não contar os Reprocessar
            nooffer_count = sum(
                1
                for r in full_results
                if r["status"] == "Sem Oferta" and r["vendido"] != REPROCESSAR_LABEL
            )

            lfo_pct = (lfo_count / total * 100.0) if total else 0.0

            summary = {
                "total": total,
                "lfo_count": lfo_count,
                "nooffer_count": nooffer_count,
                "reproc_count": reproc_count,
                "amazonfo_count": amazonfo_count,
                "lfo_pct": lfo_pct,
            }

            results_display = full_results

    return render_template_string(
        HTML_TEMPLATE,
        asin_text=asin_text,
        results=results_display,
        sec_per_asin=sec_per_asin,
        summary=summary,
        read_time=read_time,
    )


if __name__ == "__main__":
    # Local: python web.py
    # Render/Gunicorn: usa "gunicorn web:app" e ignora esse bloco
    port = int(os.environ.get("PORT", "5008"))
    app.run(host="0.0.0.0", port=port, debug=True)
