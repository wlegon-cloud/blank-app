import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Llamados Compras Estatales", layout="wide")
st.title("Llamados de Compras Estatales con Filtrado")

LOCAL_TZ = ZoneInfo("America/Montevideo")
FEED_URL = "https://www.comprasestatales.gub.uy/consultas/rss"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
COLUMNS = ["Título", "Descripción", "Fecha publicación", "Enlace", "Contenido completo"]
DETAIL_TTL = 6 * 60 * 60  # segundos que se guarda el detalle de cada llamado
DETAIL_WORKERS = 8

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


# --- Utilidades ---
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


def split_terms(raw):
    return [normalize(t.strip()) for t in raw.split(",") if t.strip()]


# --- Descarga y parseo ---
@st.cache_data(ttl=300, show_spinner="Descargando llamados...")
def fetch_feed(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.content  # bytes: deja que el parser XML respete el encoding declarado


def parse_rss(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.findall(".//item"):
        items.append({
            "Título": clean_html(item.findtext("title")),
            "Descripción": clean_html(item.findtext("description")),
            "Fecha publicación": parse_date(item.findtext("pubDate")),
            "Enlace": (item.findtext("link") or "").strip(),
            "Contenido completo": clean_html(item.findtext(CONTENT_NS)),
        })
    return items


# --- Detalle de cada llamado (ítems) ---
ITEM_RE = re.compile(
    r"[ÍI]tem\s*N[º°o]?\.?\s*(\d+)\s*((?:(?![ÍI]tem\s*N[º°o]).){1,400}?)\s*\(C[óo]d\.?\s*Art[íi]culo\s*(\d+)\)",
    re.IGNORECASE,
)
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
        resp.encoding = resp.apparent_encoding if not resp.encoding else resp.encoding
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
            for i, ((url, _), data) in enumerate(
                zip(pending, pool.map(lambda a: fetch_detail(*a), pending)), start=1
            ):
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


col_btn, _ = st.columns([1, 5])
if col_btn.button("🔄 Actualizar feed"):
    fetch_feed.clear()
    detail_store().clear()

try:
    items = parse_rss(fetch_feed(FEED_URL))
except requests.exceptions.Timeout:
    st.error("El sitio de Compras Estatales no respondió a tiempo. Probá de nuevo en unos minutos.")
    st.stop()
except requests.exceptions.RequestException as e:
    st.error(f"No se pudo acceder al feed: {e}")
    st.stop()
except ET.ParseError as e:
    st.error(f"El feed no es un XML válido: {e}")
    st.stop()

df_all = pd.DataFrame(items, columns=COLUMNS)
df_all["Fecha publicación"] = pd.to_datetime(df_all["Fecha publicación"], errors="coerce")

# --- Filtros ---
c1, c2 = st.columns(2)
include_raw = c1.text_input(
    "Incluir palabras clave (separadas por coma)",
    value=", ".join(DEFAULT_INCLUDE),
    help="Viene cargada con los rubros de Essen. Borrala para ver todos los llamados.",
)
exclude_raw = c2.text_input(
    "Excluir palabras clave (separadas por coma)",
    placeholder="Ej: arrendamiento, software",
)

c3, c4, c5 = st.columns(3)
search_detail = c3.checkbox(
    "Buscar también dentro de cada llamado (ítems y detalle)",
    value=True,
    help="Entra a la página de cada llamado y busca en sus ítems y en el resto del texto. "
         "La primera carga tarda unos segundos; después queda guardado.",
)
match_all = c4.checkbox(
    "Exigir todas las palabras de 'Incluir'",
    value=False,
    help="Si está desmarcado, alcanza con que aparezca una.",
)
whole_words = c5.checkbox(
    "Solo palabras completas (incluye plurales)",
    value=True,
    help="Con esto 'carro' encuentra 'carros' pero no 'carrocería'. "
         "Desmarcalo para buscar partes de palabras (ej: 'ignifug').",
)

include = split_terms(include_raw)
exclude = split_terms(exclude_raw)


def term_pattern(term):
    """Regex para un término ya normalizado: palabra completa + plural opcional, o fragmento."""
    if whole_words:
        return re.compile(r"\b" + re.escape(term) + r"(?:s|es)?\b")
    return re.compile(re.escape(term))


include_pats = {t: term_pattern(t) for t in include}
exclude_pats = [term_pattern(t) for t in exclude]

df_all["Ítems"] = ""
df_all["Detalle"] = ""
if search_detail:
    df_all["Ítems"], df_all["Detalle"], failed = load_details(df_all)
    if failed:
        st.warning(
            f"No se pudo leer el detalle de {failed} llamados; para esos solo se busca en "
            "título y descripción. Tocá 'Actualizar feed' para reintentar."
        )

FIELDS = ["Título", "Descripción", "Ítems", "Detalle"]
norm_fields = {f: df_all[f].fillna("").map(normalize) for f in FIELDS}
haystack = pd.concat(norm_fields.values(), axis=1).agg(" ".join, axis=1)

mask = pd.Series(True, index=df_all.index)
if include:
    hits = [haystack.map(lambda x, p=p: bool(p.search(x))) for p in include_pats.values()]
    combined = pd.concat(hits, axis=1)
    mask &= combined.all(axis=1) if match_all else combined.any(axis=1)
for p in exclude_pats:
    mask &= ~haystack.map(lambda x, p=p: bool(p.search(x)))

def where_matched(i):
    """Qué palabras aparecieron y en qué parte del llamado, ej: 'rueda (Ítems); carro (Título)'."""
    parts = []
    for term, pat in include_pats.items():
        found = [f for f in FIELDS if pat.search(norm_fields[f][i])]
        if "Ítems" in found and "Detalle" in found:
            found.remove("Detalle")  # el detalle incluye los ítems; no repetir
        if found:
            parts.append(f"{term} ({', '.join(found)})")
    return "; ".join(parts)


df_all["Coincide en"] = [where_matched(i) for i in df_all.index] if include else ""
df = df_all[mask].sort_values("Fecha publicación", ascending=False, na_position="last")
SHOW = ["Título", "Descripción", "Ítems", "Coincide en", "Fecha publicación", "Enlace"]
EXPORT = ["Título", "Descripción", "Ítems", "Coincide en", "Fecha publicación", "Enlace"]

# --- Resultados ---
st.write(f"Se encontraron **{len(df)}** de {len(df_all)} llamados después del filtrado.")

if df.empty:
    st.info("Ningún llamado coincide con los filtros.")
else:
    st.dataframe(
        df[SHOW],
        width="stretch",
        hide_index=True,
        column_config={
            "Enlace": st.column_config.LinkColumn("Enlace", display_text="Abrir"),
            "Fecha publicación": st.column_config.DatetimeColumn(
                "Fecha publicación", format="DD/MM/YYYY HH:mm"
            ),
            "Descripción": st.column_config.TextColumn("Descripción", width="large"),
            "Ítems": st.column_config.TextColumn("Ítems", width="large"),
            "Coincide en": st.column_config.TextColumn("Coincide en", width="medium"),
        },
    )

st.caption(
    "El feed RSS solo trae los llamados publicados más recientemente, no el histórico completo. "
    "La búsqueda interna lee la página de cada llamado, no los pliegos adjuntos (PDF o 7z)."
)


# --- Descargas ---
def to_csv_bytes(frame):
    # utf-8-sig agrega BOM para que Excel muestre bien las tildes
    return frame.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(frame):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Llamados")
        ws = writer.sheets["Llamados"]
        widths = {"A": 60, "B": 70, "C": 80, "D": 22, "E": 18, "F": 55}
        for col, width in widths.items():
            ws.column_dimensions[col].width = width
        for cell in ws["E"][1:]:
            cell.number_format = "DD/MM/YYYY HH:MM"
        ws.freeze_panes = "A2"
    return output.getvalue()


d1, d2, _ = st.columns([1, 1, 4])
d1.download_button(
    "Descargar CSV",
    to_csv_bytes(df[EXPORT]),
    file_name="llamados_filtrados.csv",
    mime="text/csv",
    disabled=df.empty,
)
d2.download_button(
    "Descargar Excel",
    to_excel_bytes(df[EXPORT]),
    file_name="llamados_filtrados.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    disabled=df.empty,
)
