import html
import re
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


col_btn, _ = st.columns([1, 5])
if col_btn.button("🔄 Actualizar feed"):
    fetch_feed.clear()

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
    placeholder="Ej: ruedas, puertas cortafuego, contenedores",
)
exclude_raw = c2.text_input(
    "Excluir palabras clave (separadas por coma)",
    placeholder="Ej: arrendamiento, software",
)

c3, c4 = st.columns(2)
search_content = c3.checkbox(
    "Buscar también en el contenido completo",
    value=True,
    help="Además del título y la descripción, busca en el detalle del llamado.",
)
match_all = c4.checkbox(
    "Exigir todas las palabras de 'Incluir'",
    value=False,
    help="Si está desmarcado, alcanza con que aparezca una.",
)

include = split_terms(include_raw)
exclude = split_terms(exclude_raw)

search_cols = ["Título", "Descripción"] + (["Contenido completo"] if search_content else [])
haystack = df_all[search_cols].fillna("").agg(" ".join, axis=1).map(normalize)

mask = pd.Series(True, index=df_all.index)
if include:
    hits = [haystack.str.contains(t, regex=False) for t in include]
    combined = pd.concat(hits, axis=1)
    mask &= combined.all(axis=1) if match_all else combined.any(axis=1)
for t in exclude:
    mask &= ~haystack.str.contains(t, regex=False)

df = df_all[mask].sort_values("Fecha publicación", ascending=False, na_position="last")

# --- Resultados ---
st.write(f"Se encontraron **{len(df)}** de {len(df_all)} llamados después del filtrado.")

if df.empty:
    st.info("Ningún llamado coincide con los filtros.")
else:
    st.dataframe(
        df[["Título", "Descripción", "Fecha publicación", "Enlace"]],
        width="stretch",
        hide_index=True,
        column_config={
            "Enlace": st.column_config.LinkColumn("Enlace", display_text="Abrir"),
            "Fecha publicación": st.column_config.DatetimeColumn(
                "Fecha publicación", format="DD/MM/YYYY HH:mm"
            ),
            "Descripción": st.column_config.TextColumn("Descripción", width="large"),
        },
    )

st.caption(
    "El feed RSS solo trae los llamados publicados más recientemente, no el histórico completo."
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
        widths = {"A": 60, "B": 80, "C": 18, "D": 50, "E": 80}
        for col, width in widths.items():
            ws.column_dimensions[col].width = width
        for cell in ws["C"][1:]:
            cell.number_format = "DD/MM/YYYY HH:MM"
        ws.freeze_panes = "A2"
    return output.getvalue()


d1, d2, _ = st.columns([1, 1, 4])
d1.download_button(
    "Descargar CSV",
    to_csv_bytes(df),
    file_name="llamados_filtrados.csv",
    mime="text/csv",
    disabled=df.empty,
)
d2.download_button(
    "Descargar Excel",
    to_excel_bytes(df),
    file_name="llamados_filtrados.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    disabled=df.empty,
)
