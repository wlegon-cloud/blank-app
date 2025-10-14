import streamlit as st
import pandas as pd
import requests
import xml.etree.ElementTree as ET
from io import BytesIO

st.set_page_config(page_title="Llamados Compras Estatales", layout="wide")
st.title("Llamados de Compras Estatales con Filtrado")

FEED_URL = "https://www.comprasestatales.gub.uy/consultas/rss"

@st.cache_data(ttl=300)
def fetch_feed(url):
    """Descarga el feed RSS simulando un navegador para evitar 403."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.text

def parse_rss(xml_text):
    """Parsea el XML RSS y devuelve una lista de dicts con campos relevantes."""
    root = ET.fromstring(xml_text)
    items = []
    for item in root.findall(".//item"):
        d = {
            "Título": item.findtext("title"),
            "Descripción": item.findtext("description"),
            "Fecha publicación": item.findtext("pubDate"),
            "Enlace": item.findtext("link"),
            "Contenido completo": item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
        }
        items.append(d)
    return items

# --- Descargar y parsear ---
try:
    xml = fetch_feed(FEED_URL)
    items = parse_rss(xml)
except requests.exceptions.HTTPError as e:
    st.error(f"No se pudo acceder al feed: {e}")
    st.stop()
except Exception as e:
    st.error(f"Error al procesar el feed: {e}")
    st.stop()

# --- Filtrado por palabras clave ---
keywords_input = st.text_input(
    "Filtrar por palabras clave (separadas por coma)",
    placeholder="Ej: mantenimiento, seguridad, limpieza"
)
keywords = [k.strip().lower() for k in keywords_input.split(",") if k.strip()]

if keywords:
    filtered_items = []
    for item in items:
        text = (item["Título"] or "") + " " + (item["Descripción"] or "")
        text = text.lower()
        if any(kw in text for kw in keywords):
            filtered_items.append(item)
else:
    filtered_items = items

df = pd.DataFrame(filtered_items)

st.write(f"Se encontraron {len(filtered_items)} llamados después del filtrado.")
st.dataframe(df[["Título", "Descripción", "Fecha publicación", "Enlace"]])

# --- Descarga CSV ---
def to_csv_bytes(df: pd.DataFrame):
    return df.to_csv(index=False).encode("utf-8")

csv_bytes = to_csv_bytes(df)
st.download_button(
    "Descargar como CSV",
    csv_bytes,
    file_name="llamados_filtrados.csv",
    mime="text/csv"
)

# --- Descarga Excel ---
def to_excel_bytes(df: pd.DataFrame):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Llamados")
    return output.getvalue()

excel_bytes = to_excel_bytes(df)
st.download_button(
    "Descargar como Excel",
    excel_bytes,
    file_name="llamados_filtrados.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
