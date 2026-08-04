import os
import time
import json
import html
import logging
import requests
from datetime import datetime
from deep_translator import GoogleTranslator

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("global_news_bot")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

ARCHIVO_HISTORIAL = "noticias_enviadas.json"
MAX_HISTORIAL = 300
MAX_NOTICIAS_GLOBALES = 12
MAX_LARGO_MENSAJE = 3500

url_telegram = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
url_newsapi = "https://newsapi.org/v2/everything"

# Firmas que indican que el traductor devolvió una página de error
# de Google (HTTP 200 con cuerpo de error) en vez de una traducción real.
FIRMAS_ERROR_TRADUCCION = [
    "that's an error",
    "there was an error",
    "please try again later",
    "that's all we know",
    "error 500",
    "error 429",
    "<html", "<!doctype"
]

# CONSULTAS ESPECIALES
QUERY_IRAN = (
    "(Iran OR Iranian OR Tehran OR \"Iran conflict\" OR \"Israel Iran\" OR "
    "\"US Iran\" OR \"Middle East conflict\" OR \"Strait of Hormuz\")"
)

QUERY_TRUMP = (
    "(Trump OR \"Donald Trump\" OR \"Trump administration\" OR "
    "\"Trump policy\" OR \"Trump tariffs\" OR \"Trump election\")"
)

QUERY_GLOBAL = (
    "(AI OR economy OR geopolitics OR war OR energy OR technology OR "
    "semiconductor OR inflation OR china OR russia OR markets OR "
    "bitcoin OR crypto OR cryptocurrency OR ethereum OR blockchain OR "
    "binance OR defi OR nft OR stablecoin OR altcoins OR web3 OR "
    "regulation OR etf OR tokenization)"
)

DOMINIOS = (
    "reuters.com,bbc.com,bloomberg.com,theverge.com,cnn.com,"
    "coindesk.com,cointelegraph.com,theblock.co"
)

traductor = GoogleTranslator(source="auto", target="es")


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------

def cargar_historial():
    if not os.path.exists(ARCHIVO_HISTORIAL):
        return []

    try:
        with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
            historial = json.load(f)
            return historial if isinstance(historial, list) else []

    except Exception as error:
        log.error(f"Error leyendo historial, se respalda y reinicia: {error}")
        try:
            os.replace(ARCHIVO_HISTORIAL, f"{ARCHIVO_HISTORIAL}.bak_{int(time.time())}")
        except OSError:
            pass
        return []


def guardar_historial(historial):
    historial_limpio = list(dict.fromkeys(historial))[-MAX_HISTORIAL:]

    with open(ARCHIVO_HISTORIAL, "w", encoding="utf-8") as f:
        json.dump(historial_limpio, f, ensure_ascii=False, indent=4)

    log.info(f"Historial guardado: {len(historial_limpio)} noticias")


# ---------------------------------------------------------------------------
# Traducción con validación y reintentos
# ---------------------------------------------------------------------------

def _parece_error_de_traduccion(texto):
    if not texto:
        return False

    texto_lower = texto.lower()
    return any(firma in texto_lower for firma in FIRMAS_ERROR_TRADUCCION)


def traducir(texto, max_intentos=3, espera_base=2):
    """Traduce con reintentos y backoff exponencial. Nunca lanza excepción
    hacia el llamador: si todo falla, retorna el texto original."""
    if not texto:
        return ""

    for intento in range(1, max_intentos + 1):
        try:
            resultado = traductor.translate(texto)

            if resultado and not _parece_error_de_traduccion(resultado):
                return resultado

            log.warning(
                f"Traducción inválida (intento {intento}/{max_intentos}), "
                f"firma de error detectada."
            )

        except Exception as error:
            log.warning(f"Fallo de traducción (intento {intento}/{max_intentos}): {error}")

        if intento < max_intentos:
            time.sleep(espera_base * (2 ** (intento - 1)))

    log.error("No se pudo traducir tras varios intentos. Se usa texto original.")
    return texto


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------

def obtener_noticias(query, page_size=20):
    """Consulta NewsAPI con manejo de errores de red/timeout/JSON.
    Retorna lista vacía ante cualquier fallo, sin interrumpir el script."""
    params = {
        "q": query,
        "domains": DOMINIOS,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": NEWS_API_KEY
    }

    try:
        response = requests.get(url_newsapi, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

    except requests.exceptions.RequestException as error:
        log.error(f"Error de red consultando NewsAPI: {error}")
        return []
    except ValueError as error:
        log.error(f"Respuesta de NewsAPI no es JSON válido: {error}")
        return []

    if data.get("status") != "ok":
        log.error(f"NewsAPI devolvió error: {data.get('message', data)}")
        return []

    return data.get("articles", [])


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def enviar_telegram(texto):
    """Envía un mensaje a Telegram. Retorna True solo si Telegram confirma
    la entrega (HTTP 200 + ok:true en el payload)."""
    try:
        response = requests.post(
            url_telegram,
            data={
                "chat_id": CHAT_ID,
                "text": texto[:MAX_LARGO_MENSAJE],
                "parse_mode": "HTML"
            },
            timeout=20
        )

        log.info(f"Telegram status: {response.status_code}")

        if response.status_code != 200:
            log.error(f"Telegram respondió con error: {response.text}")
            return False

        payload = response.json()
        if not payload.get("ok", False):
            log.error(f"Telegram ok=false: {payload}")
            return False

        return True

    except requests.exceptions.RequestException as error:
        log.error(f"Excepción enviando a Telegram: {error}")
        return False

    finally:
        time.sleep(1)


def preparar_mensaje(art):
    titulo = art.get("title") or "Sin título"
    descripcion = art.get("description") or "Sin descripción disponible."
    link = art.get("url") or "Sin link"

    titulo_es = traducir(titulo)
    descripcion_es = traducir(descripcion)

    titulo_es = html.escape(titulo_es)
    descripcion_es = html.escape(descripcion_es)
    link_escapado = html.escape(link)

    mensaje = f"<b>{titulo_es}</b>\n\n{descripcion_es}\n\nLink: {link_escapado}\n"

    return mensaje


# ---------------------------------------------------------------------------
# Envío
# ---------------------------------------------------------------------------

def enviar_primera_noticia_disponible(articulos, enviadas_set, enviados_en_esta_corrida):
    """Envía la primera noticia disponible que no esté en el historial.
    Solo se marca como enviada (historial + set de esta corrida) si
    Telegram confirma la entrega."""
    for art in articulos:
        link = art.get("url") or ""

        if not link:
            continue

        if link in enviadas_set:
            continue

        if link in enviados_en_esta_corrida:
            continue

        mensaje = preparar_mensaje(art)
        exito = enviar_telegram(mensaje)

        if exito:
            enviadas_set.add(link)
            enviados_en_esta_corrida.add(link)
            return link

        log.warning(f"No se pudo enviar (se reintentará en próxima corrida): {link}")
        # No se marca como usada en esta corrida: se prueba el siguiente artículo.

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        log.error("Falta configurar TOKEN.")
        return

    if not CHAT_ID:
        log.error("Falta configurar CHAT_ID.")
        return

    if not NEWS_API_KEY:
        log.error("Falta configurar NEWS_API_KEY.")
        return

    fecha_hoy = datetime.now().strftime("%d/%m/%Y")

    historial = cargar_historial()
    enviadas_set = set(historial)

    log.info("Consultando NewsAPI...")
    articulos_iran = obtener_noticias(QUERY_IRAN, page_size=10)
    articulos_trump = obtener_noticias(QUERY_TRUMP, page_size=10)
    articulos_global = obtener_noticias(QUERY_GLOBAL, page_size=60)

    if not articulos_iran and not articulos_trump and not articulos_global:
        log.info("No se obtuvieron artículos de ninguna consulta. No se publicará nada.")
        return

    # ENCABEZADO
    intro = f"<b>GLOBAL NEWS</b>\n\n<b>{fecha_hoy}</b>\n"
    enviar_telegram(intro)

    enviados_en_esta_corrida = set()
    nuevos_links = []
    contador = 0

    # 1 noticia de Irán
    link = enviar_primera_noticia_disponible(articulos_iran, enviadas_set, enviados_en_esta_corrida)
    if link:
        nuevos_links.append(link)
        contador += 1

    # 1 noticia de Trump
    link = enviar_primera_noticia_disponible(articulos_trump, enviadas_set, enviados_en_esta_corrida)
    if link:
        nuevos_links.append(link)
        contador += 1

    # Completar hasta MAX_NOTICIAS_GLOBALES con globales + cripto
    for art in articulos_global:
        if contador >= MAX_NOTICIAS_GLOBALES:
            break

        link = art.get("url") or ""

        if not link or link in enviadas_set or link in enviados_en_esta_corrida:
            continue

        mensaje = preparar_mensaje(art)
        exito = enviar_telegram(mensaje)

        if exito:
            enviadas_set.add(link)
            enviados_en_esta_corrida.add(link)
            nuevos_links.append(link)
            contador += 1
        else:
            log.warning(f"No se pudo enviar (se reintentará en próxima corrida): {link}")

    if nuevos_links:
        historial.extend(nuevos_links)
        guardar_historial(historial)

    log.info(f"Total enviadas: {contador}")


if __name__ == "__main__":
    main()
