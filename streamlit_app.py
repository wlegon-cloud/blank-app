import html
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Llamados Compras Estatales", page_icon="📋", layout="wide")

# ============================================================
# Configuración
# ============================================================
LOCAL_TZ = ZoneInfo("America/Montevideo")
FEED_URL = "https://www.comprasestatales.gub.uy/consultas/rss"
DETAIL_TTL = 6 * 60 * 60  # segundos que se guarda el detalle de cada llamado
DETAIL_WORKERS = 8
PAGE_SIZE = 20  # tarjetas que se muestran antes de "Mostrar más"

# Palabras clave de los rubros de Essen. Editá esta lista para cambiar lo que aparece por defecto.
DEFAULT_INCLUDE = [
    "rueda", "garrucha", "carro", "carrito", "zorra", "transpaleta",
    "puerta cortafuego", "cortafuego", "ignifuga", "antipanico",
    "contenedor", "papelera", "estante", "estanteria", "rack",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
}

# ============================================================
# Estilos
# ============================================================
st.markdown(
    """
<style>
.block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1200px;}
h1.app-title {font-size: 1.75rem; font-weight: 700; margin: 0; color: #0f172a;}
p.app-sub {color: #64748b; margin: .25rem 0 1.5rem 0; font-size: .95rem;}

.stats {display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 1.5rem;}
.stat {background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px 18px;}
.stat .n {font-size: 1.75rem; font-weight: 700; line-height: 1.1; color: #0f172a;}
.stat .l {font-size: .85rem; color: #64748b; margin-top: 2px;}
.stat.red .n {color: #dc2626;}
.stat.amber .n {color: #d97706;}

.results-head {color: #475569; font-size: .95rem; margin: .25rem 0 .75rem 0;}

.card {background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
       padding: 16px 20px; margin-bottom: 12px;}
.card:hover {border-color: #cbd5e1; box-shadow: 0 1px 3px rgba(15,23,42,.06);}
.card-top {display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 6px;}
.badge {font-size: .78rem; font-weight: 600; padding: 3px 10px; border-radius: 999px; white-space: nowrap;}
.badge.red {background: #fee2e2; color: #b91c1c;}
.badge.amber {background: #fef3c7; color: #92400e;}
.badge.green {background: #dcfce7; color: #166534;}
.badge.gray {background: #f1f5f9; color: #475569;}
.ref {font-size: .82rem; color: #64748b;}
.org {font-size: 1.02rem; font-weight: 600; color: #0f172a; margin-bottom: 4px;}
.desc {font-size: .93rem; color: #334155; margin-bottom: 10px;}
.chips {display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px;}
.chip {font-size: .8rem; padding: 3px 9px; border-radius: 6px; background: #f1f5f9; color: #475569;}
.chip.hit {background: #e0e7ff; color: #3730a3; font-weight: 500;}
.chip.more {background: transparent; color: #94a3b8; padding-left: 2px;}
.card-foot {display: flex; justify-content: space-between; align-items: center; gap: 12px;
            flex-wrap: wrap; border-top: 1px solid #f1f5f9; padding-top: 10px; font-size: .82rem; color: #64748b;}
.card-foot .terms b {color: #3730a3; font-weight: 600;}
.card-foot .weak {color: #94a3b8; font-style: italic;}
.card-foot a {color: #2563eb; font-weight: 600; text-decoration: none; white-space: nowrap;}
.card-foot a:hover {text-decoration: underline;}
</style>
""",
    unsafe_allow_html=True,
)


# ============================================================
# Utilidades
# ============================================================
def clean_html(text):
    """Quita etiquetas HTML, decodifica entidades y compacta espacios."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize(text):
    """Minúsculas y sin tildes, para que 'construccion' encuentre 'construcción'."""
    text = unicodedata.normalize("NFKD", text or "")
    return text.encode("ascii", "ignore").decode("ascii").lower()


def tidy_case(text):
    """Pasa textos EN MAYÚSCULAS a formato oración, que se lee más limpio."""
    text = (text or "").strip()
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return text[:1].upper() + text[1:].lower()
    return text


def parse_date(value):
    """Convierte la fecha RSS (RFC 822) a hora de Montevideo, sin tz (Excel no acepta tz)."""
    if not value:
        return pd.NaT
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return pd.NaT
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return dt


CIERRE_RE = re.compile(
    r"Recepci[óo]n de ofertas hasta:?\s*(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}))?", re.IGNORECASE
)
META_RE = re.compile(r"\s*(Recepci[óo]n de ofertas hasta|Publicado):.*$", re.IGNORECASE)
TITLE_RE = re.compile(r"^(.*?\d+/\d{4})\s*-\s*(.+)$")


def parse_cierre(desc):
    """Saca la fecha de 'Recepción de ofertas hasta: 17/03/2027 11:00hs' de la descripción."""
    m = CIERRE_RE.search(desc or "")
    if not m:
        return pd.NaT
    return pd.to_datetime(f"{m.group(1)} {m.group(2) or '23:59'}", format="%d/%m/%Y %H:%M", errors="coerce")


def split_title(title):
    """'Compra Directa 38/2026 - Banco Hipotecario' -> ('Compra Directa 38/2026', 'Banco Hipotecario')."""
    m = TITLE_RE.match(title or "")
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", title or "")


def countdown(cierre, now):
    """Devuelve (texto, nivel) según lo que falta para el cierre de ofertas."""
    if pd.isna(cierre):
        return "Sin fecha de cierre", "gray"
    if cierre < now:
        return "Cerrado", "gray"
    days = (cierre.date() - now.date()).days
    if days == 0:
        return f"Cierra hoy {cierre:%H:%M}", "red"
    if days == 1:
        return f"Cierra mañana {cierre:%H:%M}", "red"
    level = "red" if days <= 3 else "amber" if days <= 7 else "green"
    return f"Faltan {days} días", level


def split_terms(raw):
    return [normalize(t.strip()) for t in raw.replace("\n", ",").split(",") if t.strip()]


# ============================================================
# Descarga del feed y del detalle de cada llamado
# ============================================================
@st.cache_data(ttl=300, show_spinner="Descargando llamados...")
def fetch_feed(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.content, datetime.now(LOCAL_TZ)  # bytes: respeta el encoding declarado en el XML


def parse_rss(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [
        {
            "Título": clean_html(item.findtext("title")),
            "Descripción": clean_html(item.findtext("description")),
            "Fecha publicación": parse_date(item.findtext("pubDate")),
            "Enlace": (item.findtext("link") or "").strip(),
        }
        for item in root.findall(".//item")
    ]


ITEM_RE = re.compile(
    r"[ÍI]tem\s*N[º°o]?\.?\s*(\d+)\s*((?:(?![ÍI]tem\s*N[º°o]).){1,400}?)\s*\(C[óo]d\.?\s*Art[íi]culo\s*(\d+)\)",
    re.IGNORECASE,
)
ITEM_TEXT_RE = re.compile(r"^\d+ · (.*) \(cód\. \d+\)$")
BOILERPLATE_RE = re.compile(
    r"<(script|style|nav|header|footer|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)


@st.cache_resource
def detail_store():
    """Guarda el detalle ya descargado entre recargas y usuarios: {url: (timestamp, datos)}."""
    return {}


def parse_detail(page_html, titulo):
    text = clean_html(BOILERPLATE_RE.sub(" ", page_html))
    # Recorta el encabezado del sitio: arranca donde aparece el número del llamado
    m = re.search(r"\d+/\d{4}", titulo or "")
    if m and m.group(0) in text:
        text = text[text.index(m.group(0)):]
    items = [f"{num} · {desc.strip()} (cód. {cod})" for num, desc, cod in ITEM_RE.findall(text)]
    return {"Ítems": " | ".join(items), "Detalle": text}


def fetch_detail(url, titulo):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.encoding or resp.apparent_encoding
        return parse_detail(resp.text, titulo)
    except Exception:
        return None  # si falla, se reintenta en la próxima carga


def load_details(frame):
    """Descarga en paralelo el detalle de los llamados que no estén en caché."""
    store = detail_store()
    now = time.time()
    pending = [
        (url, tit) for url, tit in zip(frame["Enlace"], frame["Título"])
        if url and (url not in store or now - store[url][0] > DETAIL_TTL)
    ]
    if pending:
        bar = st.progress(0.0, text=f"Leyendo el detalle de {len(pending)} llamados...")
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            results = pool.map(lambda a: fetch_detail(*a), pending)
            for i, ((url, _), data) in enumerate(zip(pending, results), start=1):
                if data is not None:
                    store[url] = (now, data)
                bar.progress(i / len(pending), text=f"Leyendo el detalle de llamados... {i}/{len(pending)}")
        bar.empty()
    failed = sum(1 for url in frame["Enlace"] if url and url not in store)
    get = lambda url, key: store[url][1][key] if url in store else ""
    return (
        frame["Enlace"].map(lambda u: get(u, "Ítems")),
        frame["Enlace"].map(lambda u: get(u, "Detalle")),
        failed,
    )


# ============================================================
# Barra lateral: filtros
# ============================================================
with st.sidebar:
    st.markdown("### Búsqueda")
    include_raw = st.text_area(
        "Palabras clave",
        value=", ".join(DEFAULT_INCLUDE),
        height=110,
        help="Separadas por coma. Vienen cargados los rubros de Essen; borralas para ver todos los llamados.",
    )
    exclude_raw = st.text_input("Excluir", placeholder="Ej: silla de rueda, servidor")
    sort_by = st.selectbox("Ordenar por", ["Cierre más próximo", "Publicación más reciente"])
    hide_closed = st.toggle("Ocultar plazos vencidos", value=True)

    with st.expander("Opciones avanzadas"):
        search_detail = st.checkbox(
            "Buscar dentro de cada llamado",
            value=True,
            help="Entra a la página de cada llamado y busca en sus ítems y en el resto del texto. "
                 "La primera carga tarda unos segundos; después queda guardado.",
        )
        whole_words = st.checkbox(
            "Solo palabras completas",
            value=True,
            help="'carro' encuentra 'carros' pero no 'carrocería'. "
                 "Desmarcalo para buscar partes de palabras (ej: 'ignifug').",
        )
        match_all = st.checkbox("Exigir todas las palabras", value=False)

    st.divider()
    if st.button("Actualizar datos", width="stretch"):
        fetch_feed.clear()
        detail_store().clear()

# ============================================================
# Datos
# ============================================================
try:
    feed_bytes, fetched_at = fetch_feed(FEED_URL)
    items = parse_rss(feed_bytes)
except requests.exceptions.Timeout:
    st.error("El sitio de Compras Estatales no respondió a tiempo. Probá de nuevo en unos minutos.")
    st.stop()
except requests.exceptions.RequestException as e:
    st.error(f"No se pudo acceder al feed: {e}")
    st.stop()
except ET.ParseError as e:
    st.error(f"El feed no es un XML válido: {e}")
    st.stop()

NOW = datetime.now(LOCAL_TZ).replace(tzinfo=None)
df_all = pd.DataFrame(items, columns=["Título", "Descripción", "Fecha publicación", "Enlace"])
df_all["Fecha publicación"] = pd.to_datetime(df_all["Fecha publicación"], errors="coerce")
df_all["Cierre de ofertas"] = pd.to_datetime(df_all["Descripción"].map(parse_cierre), errors="coerce")
df_all["Descripción"] = df_all["Descripción"].map(lambda d: META_RE.sub("", d or ""))
df_all[["Llamado", "Organismo"]] = pd.DataFrame(
    df_all["Título"].map(split_title).tolist(), index=df_all.index
) if len(df_all) else pd.DataFrame(columns=["Llamado", "Organismo"])
df_all["Faltan"] = df_all["Cierre de ofertas"].map(lambda c: countdown(c, NOW)[0])

df_all["Ítems"] = ""
df_all["Detalle"] = ""
failed = 0
if search_detail:
    df_all["Ítems"], df_all["Detalle"], failed = load_details(df_all)

# ============================================================
# Filtrado
# ============================================================
include = split_terms(include_raw)
exclude = split_terms(exclude_raw)


def term_pattern(term):
    """Regex para un término normalizado: palabra completa + plural opcional, o fragmento."""
    if whole_words:
        return re.compile(r"\b" + re.escape(term) + r"(?:s|es)?\b")
    return re.compile(re.escape(term))


include_pats = {t: term_pattern(t) for t in include}
exclude_pats = [term_pattern(t) for t in exclude]

FIELDS = ["Título", "Descripción", "Ítems", "Detalle"]
norm_fields = {f: df_all[f].fillna("").map(normalize) for f in FIELDS}
haystack = pd.concat(norm_fields.values(), axis=1).agg(" ".join, axis=1) if len(df_all) else pd.Series(dtype=str)

mask = pd.Series(True, index=df_all.index)
if include:
    hits = pd.concat([haystack.map(lambda x, p=p: bool(p.search(x))) for p in include_pats.values()], axis=1)
    mask &= hits.all(axis=1) if match_all else hits.any(axis=1)
for p in exclude_pats:
    mask &= ~haystack.map(lambda x, p=p: bool(p.search(x)))
if hide_closed:
    mask &= ~(df_all["Cierre de ofertas"] < NOW)


def match_info(i):
    """Palabras encontradas y si aparecieron solo en el texto largo (coincidencia débil)."""
    terms, strong = [], False
    for term, pat in include_pats.items():
        where = [f for f in FIELDS if pat.search(norm_fields[f][i])]
        if where:
            terms.append(term)
            strong |= any(f != "Detalle" for f in where)
    return terms, strong


info = {i: match_info(i) for i in df_all.index} if include else {}
df_all["Coincide"] = [", ".join(info[i][0]) if info else "" for i in df_all.index]

if sort_by == "Cierre más próximo":
    df = df_all[mask].sort_values("Cierre de ofertas", ascending=True, na_position="last")
else:
    df = df_all[mask].sort_values("Fecha publicación", ascending=False, na_position="last")

# ============================================================
# Encabezado y resumen
# ============================================================
st.markdown('<h1 class="app-title">Llamados de compras estatales</h1>', unsafe_allow_html=True)
st.markdown(
    f'<p class="app-sub">{len(df_all)} llamados en el feed de ARCE · '
    f'actualizado {fetched_at:%d/%m %H:%M}</p>',
    unsafe_allow_html=True,
)

days_left = (df["Cierre de ofertas"].dt.normalize() - pd.Timestamp(NOW.date())).dt.days
open_mask = df["Cierre de ofertas"] >= NOW
n_red = int((open_mask & (days_left <= 3)).sum())
n_amber = int((open_mask & days_left.between(4, 7)).sum())
st.markdown(
    f"""
<div class="stats">
  <div class="stat red"><div class="n">{n_red}</div><div class="l">Cierran en 3 días o menos</div></div>
  <div class="stat amber"><div class="n">{n_amber}</div><div class="l">Cierran en 4 a 7 días</div></div>
  <div class="stat"><div class="n">{len(df)}</div><div class="l">Llamados que coinciden</div></div>
</div>
""",
    unsafe_allow_html=True,
)

if failed:
    st.caption(
        f"⚠️ No se pudo leer el detalle de {failed} llamados; en esos solo se buscó en título "
        "y descripción. Tocá «Actualizar datos» para reintentar."
    )

# ============================================================
# Resultados
# ============================================================
head_l, head_r = st.columns([3, 1])
head_l.markdown(
    f'<div class="results-head"><b>{len(df)}</b> llamados · '
    f'{"ordenados por cierre" if sort_by == "Cierre más próximo" else "más recientes primero"}</div>',
    unsafe_allow_html=True,
)
view = head_r.segmented_control(
    "Vista", ["Tarjetas", "Tabla"], default="Tarjetas", label_visibility="collapsed"
) or "Tarjetas"


def render_card(i, row):
    esc = html.escape
    label, level = countdown(row["Cierre de ofertas"], NOW)
    terms, strong = info.get(i, ([], True))

    # Ítems: primero los que coinciden con la búsqueda, después el resto (máx. 5 en total)
    item_names = []
    for raw in filter(None, (row["Ítems"] or "").split(" | ")):
        m = ITEM_TEXT_RE.match(raw)
        item_names.append(tidy_case(m.group(1) if m else raw))
    hit_items = [n for n in item_names if any(p.search(normalize(n)) for p in include_pats.values())]
    other_items = [n for n in item_names if n not in hit_items]
    shown = (hit_items + other_items)[:5]
    chips = "".join(
        f'<span class="chip{" hit" if n in hit_items else ""}">{esc(n)}</span>' for n in shown
    )
    if len(item_names) > len(shown):
        chips += f'<span class="chip more">+{len(item_names) - len(shown)} ítems</span>'

    if terms:
        terms_html = "Coincide: " + ", ".join(f"<b>{esc(t)}</b>" for t in terms)
        if not strong:
            terms_html += ' <span class="weak">· solo en el texto del llamado</span>'
    else:
        terms_html = ""
    dates = []
    if pd.notna(row["Cierre de ofertas"]):
        dates.append(f"Cierre {row['Cierre de ofertas']:%d/%m/%Y %H:%M}")
    if pd.notna(row["Fecha publicación"]):
        dates.append(f"Publicado {row['Fecha publicación']:%d/%m}")

    return f"""
<div class="card">
  <div class="card-top">
    <span class="badge {level}">{esc(label)}</span>
    <span class="ref">{esc(row["Llamado"])}</span>
  </div>
  <div class="org">{esc(row["Organismo"])}</div>
  <div class="desc">{esc(tidy_case(row["Descripción"]))}</div>
  {f'<div class="chips">{chips}</div>' if chips else ''}
  <div class="card-foot">
    <span class="terms">{terms_html}</span>
    <span>{" · ".join(dates)} &nbsp; <a href="{esc(row["Enlace"])}" target="_blank">Ver llamado →</a></span>
  </div>
</div>"""


if df.empty:
    st.info("Ningún llamado coincide con los filtros.")
elif view == "Tarjetas":
    # "Mostrar más": vuelve a 20 cada vez que cambian los filtros
    signature = (include_raw, exclude_raw, sort_by, hide_closed, search_detail, whole_words, match_all)
    if st.session_state.get("signature") != signature:
        st.session_state.signature = signature
        st.session_state.limit = PAGE_SIZE
    limit = st.session_state.limit
    st.markdown(
        "".join(render_card(i, row) for i, row in df.head(limit).iterrows()),
        unsafe_allow_html=True,
    )
    if len(df) > limit:
        if st.button(f"Mostrar más ({len(df) - limit} restantes)"):
            st.session_state.limit += PAGE_SIZE
            st.rerun()
else:
    st.dataframe(
        df[["Faltan", "Llamado", "Organismo", "Descripción", "Coincide", "Cierre de ofertas", "Enlace"]],
        width="stretch",
        hide_index=True,
        column_config={
            "Enlace": st.column_config.LinkColumn("Enlace", display_text="Ver"),
            "Cierre de ofertas": st.column_config.DatetimeColumn("Cierre", format="DD/MM/YYYY HH:mm"),
            "Descripción": st.column_config.TextColumn("Descripción", width="large"),
        },
    )


# ============================================================
# Descargas (en la barra lateral)
# ============================================================
EXPORT = ["Faltan", "Llamado", "Organismo", "Descripción", "Ítems", "Coincide",
          "Cierre de ofertas", "Fecha publicación", "Enlace"]


def to_csv_bytes(frame):
    # utf-8-sig agrega BOM para que Excel muestre bien las tildes
    return frame.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(frame):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Llamados")
        ws = writer.sheets["Llamados"]
        widths = {"Faltan": 20, "Llamado": 28, "Organismo": 45, "Descripción": 70, "Ítems": 80,
                  "Coincide": 25, "Cierre de ofertas": 18, "Fecha publicación": 18, "Enlace": 55}
        for idx, name in enumerate(frame.columns, start=1):
            letter = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[letter].width = widths.get(name, 20)
            if name in ("Cierre de ofertas", "Fecha publicación"):
                for cell in ws[letter][1:]:
                    cell.number_format = "DD/MM/YYYY HH:MM"
        ws.freeze_panes = "A2"
    return output.getvalue()


def ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def to_ics_bytes(frame):
    """Calendario con el cierre de cada llamado y avisos 3 días y 1 día antes."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Essen//Llamados ARCE//ES", "CALSCALE:GREGORIAN"]
    for _, row in frame.dropna(subset=["Cierre de ofertas"]).iterrows():
        start = row["Cierre de ofertas"].to_pydatetime().replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        end = start + timedelta(minutes=30)
        uid = re.sub(r"\W", "", row["Enlace"] or row["Título"])[-60:]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@llamados-essen",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
            f"DTEND:{end:%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{ics_escape('Cierre de ofertas: ' + row['Título'])}",
            f"DESCRIPTION:{ics_escape(row['Descripción'] + chr(10) + chr(10) + row['Enlace'])}",
            f"URL:{row['Enlace']}",
        ]
        for trigger, label in (("-P3D", "Faltan 3 días"), ("-P1D", "Falta 1 día")):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"TRIGGER:{trigger}",
                      f"DESCRIPTION:{ics_escape(label + ' para el cierre: ' + row['Título'])}", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines).encode("utf-8")


with st.sidebar:
    st.markdown("### Descargar")
    st.download_button(
        "Excel", to_excel_bytes(df[EXPORT]), file_name="llamados_filtrados.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", disabled=df.empty, width="stretch",
    )
    st.download_button(
        "CSV", to_csv_bytes(df[EXPORT]), file_name="llamados_filtrados.csv",
        mime="text/csv", disabled=df.empty, width="stretch",
    )
    st.download_button(
        "Cierres al calendario (.ics)", to_ics_bytes(df), file_name="cierres_llamados.ics",
        mime="text/calendar", disabled=not df["Cierre de ofertas"].notna().any(),
        help="Un evento por llamado en la fecha de cierre, con avisos 3 días y 1 día antes. "
             "Se importa en Google Calendar u Outlook.",
        width="stretch",
    )
    st.caption(
        "Fuente: RSS de ARCE. Se buscan el título, la descripción y la página de cada llamado, "
        "no los pliegos adjuntos."
    )
