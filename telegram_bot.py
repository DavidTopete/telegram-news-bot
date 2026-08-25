import os
import re
import time
import json
import html
import logging
import requests
import unicodedata
from datetime import datetime

# Modulo de traduccion multi-proveedor con cache. Debe estar en el mismo
# directorio que este archivo.
from traductor import (
    traducir_estricto,
    TraduccionFallida,
    autotest,
    resumen_stats,
    guardar_cache,
    proveedores_agotados,
)

# ---------------------------------------------------------------------------
# Configuracion
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

# Con 12 noticias por corrida y 2 corridas diarias, 300 entradas cubren solo
# ~12 dias. Se sube para evitar republicar noticias de hace pocas semanas.
MAX_HISTORIAL = 2000

MAX_NOTICIAS_GLOBALES = 12
CUPO_IRAN = 1
CUPO_TRUMP = 1

MAX_LARGO_MENSAJE = 3500
MAX_LARGO_TITULO = 250
MAX_LARGO_DESCRIPCION = 900

# --- Politica de traduccion estricta ---------------------------------------
# Una noticia que no se pueda traducir NO se publica: se descarta y el bot
# sigue evaluando candidatas hasta llenar el cupo.

# Techo de candidatas evaluadas por corrida (protege coste y rate-limit).
MAX_CANDIDATOS_EVALUADOS = 60

# Circuit breaker de dos umbrales. Un solo umbral produce falsos positivos:
# cada noticia exige DOS traducciones (titulo + descripcion), asi que un 50%
# de fallo por campo se convierte en 75% de descarte por noticia y varios
# descartes seguidos ocurren por azar.
MAX_FALLOS_SIN_NINGUN_EXITO = 6     # traductor caido -> abortar
MAX_FALLOS_TRAS_UN_EXITO = 15       # degradacion parcial -> seguir buscando

# Si la descripcion no traduce pero el titulo si:
#   True  -> descarta la noticia
#   False -> publica solo con el titulo
EXIGIR_DESCRIPCION_TRADUCIDA = True

# Sin traductor no se publicaria nada en modo estricto: mejor abortar antes
# de gastar cuota de NewsAPI.
ABORTAR_SI_NO_HAY_TRADUCTOR = True

# Descarta noticias con titulo casi identico a otra ya seleccionada
# (la misma historia replicada en Reuters, BBC, CNN...).
DEDUPLICAR_POR_TITULO = True

URL_TELEGRAM = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
URL_NEWSAPI = "https://newsapi.org/v2/everything"

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


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def recortar(texto, maximo, sufijo="..."):
    if not texto or len(texto) <= maximo:
        return texto
    return texto[:maximo].rstrip() + sufijo


# Palabras vacias que no aportan identidad al titular.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "as", "at", "by", "from", "that", "this", "is", "are", "was", "were",
    "be", "it", "its", "has", "have", "after", "over", "amid", "says",
}

# Numero minimo de tokens significativos para que la firma sea fiable.
# Por debajo de esto se desactiva la deduplicacion de ese titular.
MIN_TOKENS_FIRMA = 4


def firma_titulo(titulo):
    """Normaliza un titular para detectar la misma historia replicada por
    varios medios.

    Conserva las CIFRAS deliberadamente: sin ellas, 'Bitcoin hits 90000' y
    'Bitcoin hits 70000' producirian la misma firma y el bot descartaria la
    segunda como duplicada siendo noticias distintas. Se filtran stopwords
    en lugar de filtrar por longitud, por el mismo motivo.

    Devuelve cadena vacia si el titular no da suficientes tokens fiables,
    lo que desactiva la deduplicacion para ese caso (falso negativo barato
    frente a un falso positivo que descartaria una noticia valida).
    """
    texto = unicodedata.normalize("NFKD", titulo or "")
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)

    tokens = [t for t in texto.split() if t not in STOPWORDS and len(t) > 1]

    if len(tokens) < MIN_TOKENS_FIRMA:
        return ""

    return " ".join(tokens[:10])


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------

def obtener_noticias(query, page_size=20):
    """Consulta NewsAPI. Retorna lista vacia ante cualquier fallo."""
    params = {
        "q": query,
        "domains": DOMINIOS,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": NEWS_API_KEY
    }

    try:
        response = requests.get(URL_NEWSAPI, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

    except requests.exceptions.RequestException as error:
        log.error(f"Error de red consultando NewsAPI: {error}")
        return []
    except ValueError as error:
        log.error(f"Respuesta de NewsAPI no es JSON valido: {error}")
        return []

    if data.get("status") != "ok":
        log.error(f"NewsAPI devolvio error: {data.get('message', data)}")
        return []

    articulos = data.get("articles", [])
    log.info(f"NewsAPI devolvio {len(articulos)} articulos")
    return articulos


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def enviar_telegram(texto):
    """Retorna True solo si Telegram confirma la entrega."""
    try:
        response = requests.post(
            URL_TELEGRAM,
            data={
                "chat_id": CHAT_ID,
                "text": texto[:MAX_LARGO_MENSAJE],
                "parse_mode": "HTML"
            },
            timeout=20
        )

        log.info(f"Telegram status: {response.status_code}")

        if response.status_code != 200:
            log.error(f"Telegram respondio con error: {response.text}")
            return False

        payload = response.json()
        if not payload.get("ok", False):
            log.error(f"Telegram ok=false: {payload}")
            return False

        return True

    except requests.exceptions.RequestException as error:
        log.error(f"Excepcion enviando a Telegram: {error}")
        return False

    finally:
        time.sleep(1)


# ---------------------------------------------------------------------------
# Preparacion (traduccion) y seleccion
# ---------------------------------------------------------------------------

def preparar_mensaje(articulo):
    """Traduce y arma el mensaje. Devuelve None si no es publicable."""
    titulo = articulo.get("title") or ""
    descripcion = articulo.get("description") or ""
    link = articulo.get("url") or ""

    if not titulo or not link:
        return None

    titulo_src = recortar(titulo, MAX_LARGO_TITULO, sufijo="")
    descripcion_src = recortar(descripcion, MAX_LARGO_DESCRIPCION, sufijo="")

    try:
        titulo_es = traducir_estricto(titulo_src)
    except TraduccionFallida as error:
        log.warning(f"DESCARTADA (titulo sin traducir): {titulo_src[:70]} | {error}")
        return None

    if not descripcion_src:
        descripcion_es = ""
    else:
        try:
            descripcion_es = traducir_estricto(descripcion_src)
        except TraduccionFallida as error:
            if EXIGIR_DESCRIPCION_TRADUCIDA:
                log.warning(
                    f"DESCARTADA (descripcion sin traducir): {titulo_src[:70]} | {error}"
                )
                return None
            log.warning("Descripcion sin traducir; se publica solo el titulo.")
            descripcion_es = ""

    cuerpo = html.escape(descripcion_es) if descripcion_es else "Sin descripcion disponible."

    mensaje = (
        f"<b>{html.escape(titulo_es)}</b>\n\n"
        f"{cuerpo}\n\n"
        f"Link: {html.escape(link)}\n"
    )

    return {"link": link, "titulo_original": titulo, "mensaje": mensaje}


class Selector:
    """Mantiene el estado compartido entre las tres fases de seleccion
    (Iran, Trump, globales): historial, deduplicacion y circuit breaker."""

    def __init__(self, historial):
        self.vistos = set(historial)
        self.firmas = set()
        self.evaluadas = 0
        self.descartadas = 0
        self.fallos_consecutivos = 0
        self.exitos = 0

    def _breaker_abierto(self):
        umbral = (MAX_FALLOS_TRAS_UN_EXITO if self.exitos
                  else MAX_FALLOS_SIN_NINGUN_EXITO)

        if self.fallos_consecutivos < umbral:
            return False

        if self.exitos:
            log.error(
                f"Circuit breaker: {self.fallos_consecutivos} fallos consecutivos "
                f"tras {self.exitos} exitos. Proveedor degradado a mitad de la "
                f"corrida; se publica lo obtenido."
            )
        else:
            log.error(
                f"Circuit breaker: {self.fallos_consecutivos} fallos consecutivos "
                f"sin ninguna traduccion exitosa. Traductor caido."
            )
        return True

    def seleccionar(self, articulos, cupo, etiqueta):
        """Recorre articulos traduciendo uno a uno; devuelve hasta `cupo`
        noticias publicables."""
        elegidas = []

        for articulo in articulos:
            if len(elegidas) >= cupo:
                break
            if self.evaluadas >= MAX_CANDIDATOS_EVALUADOS:
                log.warning(f"[{etiqueta}] Techo de {MAX_CANDIDATOS_EVALUADOS} "
                            f"candidatas evaluadas alcanzado.")
                break
            if self._breaker_abierto() or proveedores_agotados():
                break

            link = articulo.get("url") or ""
            if not link or link in self.vistos:
                continue

            firma = firma_titulo(articulo.get("title"))
            if DEDUPLICAR_POR_TITULO and firma and firma in self.firmas:
                log.info(f"[{etiqueta}] Duplicada por titulo: {articulo.get('title')[:60]}")
                self.vistos.add(link)
                continue

            self.evaluadas += 1
            preparada = preparar_mensaje(articulo)

            if preparada is None:
                self.descartadas += 1
                self.fallos_consecutivos += 1
                continue

            self.fallos_consecutivos = 0
            self.exitos += 1
            self.vistos.add(link)
            if firma:
                self.firmas.add(firma)

            elegidas.append(preparada)
            log.info(f"[{etiqueta}] Lista {len(elegidas)}/{cupo}: "
                     f"{preparada['titulo_original'][:70]}")

        if len(elegidas) < cupo:
            log.warning(f"[{etiqueta}] Solo {len(elegidas)}/{cupo} publicables.")

        return elegidas

    def resumen(self):
        return (f"Evaluadas: {self.evaluadas} | Publicables: {self.exitos} | "
                f"Descartadas por traduccion: {self.descartadas}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    faltantes = [n for n, v in
                 [("TOKEN", TOKEN), ("CHAT_ID", CHAT_ID), ("NEWS_API_KEY", NEWS_API_KEY)]
                 if not v]
    if faltantes:
        log.error(f"Faltan variables de entorno: {', '.join(faltantes)}")
        return

    proveedor = autotest()
    if proveedor is None and ABORTAR_SI_NO_HAY_TRADUCTOR:
        log.error(
            "Sin traductor disponible. En modo estricto no se publicaria nada; "
            "se aborta sin gastar cuota de NewsAPI ni tocar el historial."
        )
        return

    historial = cargar_historial()
    log.info(f"Historial cargado: {len(historial)} noticias")

    log.info("Consultando NewsAPI...")
    articulos_iran = obtener_noticias(QUERY_IRAN, page_size=15)
    articulos_trump = obtener_noticias(QUERY_TRUMP, page_size=15)
    articulos_global = obtener_noticias(QUERY_GLOBAL, page_size=80)

    if not (articulos_iran or articulos_trump or articulos_global):
        log.info("Ninguna consulta devolvio articulos. No se publica nada.")
        return

    # Seleccion ANTES de enviar el encabezado: asi no queda un encabezado
    # huerfano si ninguna noticia resulta publicable.
    selector = Selector(historial)

    lote = []
    lote += selector.seleccionar(articulos_iran, CUPO_IRAN, "IRAN")
    lote += selector.seleccionar(articulos_trump, CUPO_TRUMP, "TRUMP")

    restantes = MAX_NOTICIAS_GLOBALES - len(lote)
    if restantes > 0:
        lote += selector.seleccionar(articulos_global, restantes, "GLOBAL")

    guardar_cache()
    log.info(selector.resumen())

    if not lote:
        log.warning("Ninguna noticia resulto publicable. No se envia nada.")
        log.info(resumen_stats())
        return

    enviar_telegram(
        f"<b>GLOBAL NEWS</b>\n\n<b>{datetime.now().strftime('%d/%m/%Y')}</b>\n"
    )

    enviadas = 0
    fallidas = 0

    for noticia in lote:
        if enviar_telegram(noticia["mensaje"]):
            # Guardado incremental: si el job muere a mitad, lo ya enviado
            # queda registrado y no se republica.
            historial.append(noticia["link"])
            guardar_historial(historial)
            enviadas += 1
        else:
            fallidas += 1
            log.warning(f"No se pudo enviar (se reintentara): {noticia['link']}")

    log.info(f"Total enviadas: {enviadas} | Fallidas: {fallidas}")
    log.info(resumen_stats())


if __name__ == "__main__":
    main()
