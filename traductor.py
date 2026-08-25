"""
traductor.py
--------------------------------------------------------------------------
Modulo de traduccion resiliente para bots sin supervision.

Principios de diseno:
  1. Multi-proveedor en cascada: si Google bloquea tu IP, otro responde.
  2. Chunking por proveedor: cada API tiene su propio limite de caracteres.
  3. Cache persistente en disco: no se re-consume cuota en reintentos ni
     entre corridas. Es la mitigacion mas efectiva contra el rate-limit.
  4. Fallo explicito: nunca devuelve HTML de error disfrazado de traduccion.

Configuracion por variables de entorno (todas opcionales):
    TRADUCTOR_ORDEN        deepl,libretranslate,mymemory,lingva,google_clients5,google_gtx
    DEEPL_API_KEY          clave DeepL (free termina en ':fx') -- 500k chars/mes
    LIBRETRANSLATE_URL     ej. http://localhost:5000  (self-host = sin limite)
    LIBRETRANSLATE_API_KEY opcional
    MYMEMORY_EMAIL         sube el limite anonimo de ~5k a ~50k chars/dia
    LINGVA_URL             default https://lingva.ml
    HTTPS_PROXY            proxy de salida (evade bloqueo por IP)
"""

import os
import re
import json
import time
import hashlib
import logging
import urllib.parse

import requests

log = logging.getLogger("traductor")

IDIOMA_ORIGEN = "en"
IDIOMA_DESTINO = "es"

TIMEOUT = 20
MAX_INTENTOS = 2                 # por proveedor; la cascada aporta el resto
ESPERA_BASE = 2                  # backoff: 2s, 4s
PAUSA_ENTRE_CHUNKS = 1.0

ARCHIVO_CACHE = os.getenv("ARCHIVO_CACHE_TRADUCCION", "cache_traducciones.json")
MAX_ENTRADAS_CACHE = 5000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

FIRMAS_ERROR = [
    "unusual traffic", "detected unusual", "our systems have detected",
    "that's an error", "there was an error", "please try again later",
    "that's all we know", "error 500", "error 429",
    "<html", "<!doctype", "captcha",
]

ORDEN_DEFAULT = [
    "deepl",             # con key: el mas confiable
    "libretranslate",    # self-host: inmune a rate-limit externo
    "mymemory",          # gratis sin key
    "lingva",            # proxy comunitario de Google
    "google_clients5",   # endpoint alterno, menos rate-limitado
    "google_gtx",        # endpoint clasico
    "deep_translator",   # ultimo recurso
]

STATS = {"cache_hits": 0, "traducciones": 0, "fallos": 0, "proveedor_usado": {}}

# Proveedores que ya fallaron con error no recuperable en esta corrida:
# se saltan para no gastar tiempo ni empeorar el rate-limit.
_PROVEEDORES_QUEMADOS = set()


class TraduccionFallida(Exception):
    pass


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache = None


def _clave_cache(texto):
    base = f"{IDIOMA_ORIGEN}|{IDIOMA_DESTINO}|{texto}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def cargar_cache():
    global _cache
    if _cache is not None:
        return _cache

    if not os.path.exists(ARCHIVO_CACHE):
        _cache = {}
        return _cache

    try:
        with open(ARCHIVO_CACHE, "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
        _cache = datos if isinstance(datos, dict) else {}
        log.info(f"Cache de traducciones: {len(_cache)} entradas")
    except Exception as error:
        log.warning(f"Cache ilegible, se reinicia: {error}")
        _cache = {}

    return _cache


def guardar_cache():
    if _cache is None:
        return

    datos = _cache
    if len(datos) > MAX_ENTRADAS_CACHE:
        datos = dict(list(datos.items())[-MAX_ENTRADAS_CACHE:])

    try:
        with open(ARCHIVO_CACHE, "w", encoding="utf-8") as archivo:
            json.dump(datos, archivo, ensure_ascii=False, indent=2)
    except Exception as error:
        log.warning(f"No se pudo guardar la cache: {error}")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _parece_error(texto):
    if not texto:
        return True
    bajo = str(texto).lower()
    return any(firma in bajo for firma in FIRMAS_ERROR)


def _validar(resultado):
    if not resultado or not str(resultado).strip():
        raise TraduccionFallida("respuesta vacia")
    if _parece_error(resultado):
        raise TraduccionFallida("respuesta con firma de error / bloqueo")
    return str(resultado).strip()


def dividir_en_chunks(texto, limite):
    """Corta en fin de oracion; si una oracion excede el limite, corta en
    espacio; solo como ultimo recurso hace corte duro."""
    if len(texto) <= limite:
        return [texto]

    piezas = re.split(r"(?<=[.!?])\s+", texto)
    chunks, actual = [], ""

    for pieza in piezas:
        while len(pieza) > limite:
            corte = pieza.rfind(" ", 0, limite)
            corte = corte if corte > limite * 0.6 else limite
            if actual:
                chunks.append(actual)
                actual = ""
            chunks.append(pieza[:corte].strip())
            pieza = pieza[corte:].strip()

        if not actual:
            actual = pieza
        elif len(actual) + 1 + len(pieza) <= limite:
            actual = f"{actual} {pieza}"
        else:
            chunks.append(actual)
            actual = pieza

    if actual:
        chunks.append(actual)

    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------------

def _p_deepl(texto):
    clave = os.getenv("DEEPL_API_KEY")
    if not clave:
        raise TraduccionFallida("DEEPL_API_KEY no configurada")

    url = ("https://api-free.deepl.com/v2/translate"
           if clave.endswith(":fx")
           else "https://api.deepl.com/v2/translate")

    r = requests.post(
        url,
        headers={"Authorization": f"DeepL-Auth-Key {clave}"},
        data={
            "text": texto,
            "target_lang": IDIOMA_DESTINO.upper(),
            "source_lang": IDIOMA_ORIGEN.upper(),
        },
        timeout=TIMEOUT,
    )

    if r.status_code == 456:
        raise TraduccionFallida("DeepL: cuota mensual agotada")
    if r.status_code in (401, 403):
        raise TraduccionFallida(f"DeepL: clave invalida (HTTP {r.status_code})")
    r.raise_for_status()

    return _validar(r.json()["translations"][0]["text"])


def _p_libretranslate(texto):
    base = os.getenv("LIBRETRANSLATE_URL")
    if not base:
        raise TraduccionFallida("LIBRETRANSLATE_URL no configurada")

    carga = {
        "q": texto,
        "source": IDIOMA_ORIGEN,
        "target": IDIOMA_DESTINO,
        "format": "text",
    }
    if os.getenv("LIBRETRANSLATE_API_KEY"):
        carga["api_key"] = os.getenv("LIBRETRANSLATE_API_KEY")

    r = requests.post(f"{base.rstrip('/')}/translate", json=carga, timeout=TIMEOUT)
    r.raise_for_status()
    return _validar(r.json().get("translatedText"))


def _p_mymemory(texto):
    parametros = {"q": texto, "langpair": f"{IDIOMA_ORIGEN}|{IDIOMA_DESTINO}"}
    if os.getenv("MYMEMORY_EMAIL"):
        parametros["de"] = os.getenv("MYMEMORY_EMAIL")

    r = requests.get(
        "https://api.mymemory.translated.net/get",
        params=parametros, headers=HEADERS, timeout=TIMEOUT,
    )
    r.raise_for_status()
    datos = r.json()

    estado = str(datos.get("responseStatus", ""))
    if estado != "200":
        detalle = datos.get("responseDetails", "")
        if "quota" in str(detalle).lower() or estado == "429":
            raise TraduccionFallida(f"MyMemory: cuota diaria agotada ({detalle})")
        raise TraduccionFallida(f"MyMemory responseStatus={estado}: {detalle}")

    return _validar((datos.get("responseData") or {}).get("translatedText"))


def _p_lingva(texto):
    base = os.getenv("LINGVA_URL", "https://lingva.ml").rstrip("/")
    url = f"{base}/api/v1/{IDIOMA_ORIGEN}/{IDIOMA_DESTINO}/{urllib.parse.quote(texto)}"

    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return _validar(r.json().get("translation"))


def _p_google_clients5(texto):
    r = requests.get(
        "https://clients5.google.com/translate_a/t",
        params={
            "client": "dict-chrome-ex",
            "sl": IDIOMA_ORIGEN,
            "tl": IDIOMA_DESTINO,
            "q": texto,
        },
        headers=HEADERS, timeout=TIMEOUT,
    )

    if r.status_code == 429:
        raise TraduccionFallida("Google clients5: HTTP 429 (bloqueo por IP)")
    r.raise_for_status()

    datos = r.json()
    if isinstance(datos, list) and datos:
        primero = datos[0]
        resultado = primero[0] if isinstance(primero, list) else primero
    elif isinstance(datos, dict):
        resultado = datos.get("sentences", [{}])[0].get("trans", "")
    else:
        resultado = str(datos)

    return _validar(resultado)


def _p_google_gtx(texto):
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={
            "client": "gtx",
            "sl": IDIOMA_ORIGEN,
            "tl": IDIOMA_DESTINO,
            "dt": "t",
            "q": texto,
        },
        headers=HEADERS, timeout=TIMEOUT,
    )

    if r.status_code == 429:
        raise TraduccionFallida("Google gtx: HTTP 429 (bloqueo por IP)")
    r.raise_for_status()

    datos = r.json()
    if not datos or not isinstance(datos, list) or not datos[0]:
        raise TraduccionFallida("payload inesperado")

    return _validar("".join(f[0] for f in datos[0] if f and f[0]))


def _p_deep_translator(texto):
    try:
        from deep_translator import GoogleTranslator
    except Exception as error:
        raise TraduccionFallida(f"deep_translator no disponible: {error}")

    resultado = GoogleTranslator(source=IDIOMA_ORIGEN, target=IDIOMA_DESTINO).translate(texto)
    resultado = _validar(resultado)

    if resultado.strip().lower() == texto.strip().lower():
        raise TraduccionFallida("devolvio el texto sin traducir")

    return resultado


# nombre -> (funcion, limite_chars, errores_que_queman_el_proveedor)
PROVEEDORES = {
    "deepl":            (_p_deepl,            4000,  ["clave invalida", "no configurada", "cuota"]),
    "libretranslate":   (_p_libretranslate,   4000,  ["no configurada"]),
    "mymemory":         (_p_mymemory,          450,  ["cuota diaria"]),
    "lingva":           (_p_lingva,           2000,  []),
    "google_clients5":  (_p_google_clients5,  1800,  ["bloqueo por IP"]),
    "google_gtx":       (_p_google_gtx,       4500,  ["bloqueo por IP"]),
    "deep_translator":  (_p_deep_translator,  4500,  ["no disponible"]),
}


def _orden_proveedores():
    crudo = os.getenv("TRADUCTOR_ORDEN")
    if crudo:
        orden = [p.strip() for p in crudo.split(",") if p.strip() in PROVEEDORES]
        if orden:
            return orden
    return ORDEN_DEFAULT


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

def _traducir_con_proveedor(nombre, texto_completo):
    funcion, limite, quemantes = PROVEEDORES[nombre]
    chunks = dividir_en_chunks(texto_completo, limite)
    partes = []

    for indice, chunk in enumerate(chunks, start=1):
        ultimo_error = None

        for intento in range(1, MAX_INTENTOS + 1):
            try:
                partes.append(funcion(chunk))
                ultimo_error = None
                break
            except Exception as error:
                ultimo_error = error
                mensaje = str(error).lower()

                if any(q in mensaje for q in quemantes):
                    _PROVEEDORES_QUEMADOS.add(nombre)
                    raise TraduccionFallida(f"{nombre} descartado: {error}")

                if intento < MAX_INTENTOS:
                    time.sleep(ESPERA_BASE * (2 ** (intento - 1)))

        if ultimo_error is not None:
            raise TraduccionFallida(
                f"{nombre} fallo en chunk {indice}/{len(chunks)}: {ultimo_error}"
            )

        if len(chunks) > 1:
            time.sleep(PAUSA_ENTRE_CHUNKS)

    return " ".join(partes).strip()


def _traducir_interno(texto, usar_cache=True):
    """Motor comun. Devuelve (exito: bool, resultado: str).
    En caso de fallo total, resultado es el texto original."""
    if not texto or not texto.strip():
        return True, ""

    cache = cargar_cache() if usar_cache else {}
    clave = _clave_cache(texto)

    if usar_cache and clave in cache:
        STATS["cache_hits"] += 1
        return True, cache[clave]

    for nombre in _orden_proveedores():
        if nombre in _PROVEEDORES_QUEMADOS:
            continue

        try:
            resultado = _traducir_con_proveedor(nombre, texto)

            STATS["traducciones"] += 1
            STATS["proveedor_usado"][nombre] = STATS["proveedor_usado"].get(nombre, 0) + 1
            log.info(f"Traducido con '{nombre}' ({len(texto)} chars)")

            # Solo se cachean exitos: un fallo debe poder reintentarse
            # en la siguiente corrida.
            if usar_cache:
                cache[clave] = resultado
                guardar_cache()

            return True, resultado

        except Exception as error:
            log.warning(f"Proveedor '{nombre}' no sirvio: {error}")

    STATS["fallos"] += 1
    log.error(f"Todos los proveedores fallaron ({len(texto)} chars).")
    return False, texto


def traducir(texto, usar_cache=True):
    """Modo tolerante: si todos los proveedores fallan, devuelve el texto
    original (nunca basura ni HTML de error)."""
    _, resultado = _traducir_interno(texto, usar_cache)
    return resultado


def traducir_estricto(texto, usar_cache=True):
    """Modo estricto: lanza TraduccionFallida si no se pudo traducir.
    Lo usa el bot para descartar noticias no traducibles en vez de
    publicarlas en el idioma original."""
    exito, resultado = _traducir_interno(texto, usar_cache)

    if not exito:
        raise TraduccionFallida(
            f"texto de {len(texto)} chars no traducible por ningun proveedor"
        )

    return resultado


def proveedores_agotados():
    """True si todos los proveedores configurados quedaron quemados.
    Permite abortar la corrida en vez de recorrer todos los feeds en vano."""
    return all(p in _PROVEEDORES_QUEMADOS for p in _orden_proveedores())


def autotest():
    """Prueba de humo. Devuelve el nombre del proveedor que funciono, o None."""
    muestra = "The bass player recorded a new album."

    for nombre in _orden_proveedores():
        if nombre in _PROVEEDORES_QUEMADOS:
            continue
        try:
            resultado = _traducir_con_proveedor(nombre, muestra)
            log.info(f"Autotest OK con '{nombre}' -> '{resultado}'")
            return nombre
        except Exception as error:
            log.warning(f"Autotest: '{nombre}' fallo -> {error}")

    log.error(
        "Autotest FALLIDO en todos los proveedores. "
        "Probable bloqueo de IP o sin salida a internet. "
        "Ejecuta diagnostico_traduccion.py en este mismo entorno."
    )
    return None


def resumen_stats():
    return (
        f"Traducciones: {STATS['traducciones']} | "
        f"Cache hits: {STATS['cache_hits']} | "
        f"Fallos: {STATS['fallos']} | "
        f"Proveedores: {STATS['proveedor_usado'] or '-'}"
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ganador = autotest()
    print(f"\nProveedor funcional: {ganador or 'NINGUNO'}")
    print(resumen_stats())
