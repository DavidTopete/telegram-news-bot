"""
diagnostico_traduccion.py
--------------------------------------------------------------------------
Ejecutar EN EL MISMO entorno donde corre el bot (mismo runner / VPS / cron).

    python diagnostico_traduccion.py

Objetivo: determinar en qué capa está la falla antes de tocar el bot.
No requiere API keys. Si tienes DEEPL_API_KEY o LIBRETRANSLATE_URL en el
entorno, también los prueba.
"""

import os
import json
import socket
import platform
import urllib.parse

import requests

TEXTO_PRUEBA = "The bass player recorded a new album with a fretless bass."
TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

VERDE = "\033[92m"
ROJO = "\033[91m"
AMARILLO = "\033[93m"
RESET = "\033[0m"


def _ok(msg):
    print(f"{VERDE}[ OK ]{RESET} {msg}")


def _fail(msg):
    print(f"{ROJO}[FALLA]{RESET} {msg}")


def _warn(msg):
    print(f"{AMARILLO}[AVISO]{RESET} {msg}")


def _seccion(titulo):
    print("\n" + "=" * 74)
    print(titulo)
    print("=" * 74)


def _resumen_cuerpo(texto, largo=180):
    plano = " ".join(str(texto).split())
    return plano[:largo] + ("..." if len(plano) > largo else "")


def _analizar_respuesta(respuesta):
    """Clasifica una respuesta HTTP para identificar bloqueo por IP."""
    cuerpo = respuesta.text
    cuerpo_lower = cuerpo.lower()

    bloqueo = any(
        firma in cuerpo_lower
        for firma in [
            "unusual traffic",
            "detected unusual",
            "our systems have detected",
            "captcha",
            "that's an error",
            "sorry/index",
        ]
    )

    es_html = cuerpo_lower.lstrip().startswith(("<html", "<!doctype"))
    return bloqueo, es_html, cuerpo


# ---------------------------------------------------------------------------
# 0. Entorno
# ---------------------------------------------------------------------------

def entorno():
    _seccion("0. ENTORNO")
    print(f"Python      : {platform.python_version()}")
    print(f"Sistema     : {platform.system()} {platform.release()}")
    print(f"Hostname    : {socket.gethostname()}")

    en_ci = any(
        os.getenv(v)
        for v in ["GITHUB_ACTIONS", "CI", "GITLAB_CI", "RENDER", "RAILWAY_ENVIRONMENT"]
    )
    if en_ci:
        _warn("Detectado entorno CI/PaaS -> IP de datacenter compartida "
              "(causa #1 de bloqueo por Google).")

    for var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        if os.getenv(var):
            print(f"{var:<12}: {os.getenv(var)}")

    try:
        import deep_translator
        print(f"deep_translator: {getattr(deep_translator, '__version__', 'desconocida')}")
    except Exception as error:
        _warn(f"deep_translator no importable: {error}")

    print(f"requests    : {requests.__version__}")


# ---------------------------------------------------------------------------
# 1. Conectividad y IP de salida
# ---------------------------------------------------------------------------

def conectividad():
    _seccion("1. CONECTIVIDAD / IP DE SALIDA")

    try:
        respuesta = requests.get("https://api.ipify.org?format=json",
                                 headers=HEADERS, timeout=TIMEOUT)
        ip = respuesta.json().get("ip")
        _ok(f"Egress funcional. IP publica: {ip}")
    except Exception as error:
        _fail(f"Sin salida a internet o TLS roto: {error}")
        print("  -> Capa D. Revisa firewall, proxy corporativo o certificados CA.")
        return None

    # Clasificacion ASN (informativo, best-effort)
    try:
        info = requests.get(f"https://ipinfo.io/{ip}/json",
                            headers=HEADERS, timeout=TIMEOUT).json()
        org = info.get("org", "?")
        print(f"  ASN/Org   : {org}")
        print(f"  Ubicacion : {info.get('city','?')}, {info.get('country','?')}")

        marcas_datacenter = ["microsoft", "azure", "amazon", "aws", "google",
                             "digitalocean", "hetzner", "ovh", "oracle",
                             "linode", "vultr", "cloudflare"]
        if any(m in org.lower() for m in marcas_datacenter):
            _warn("IP de datacenter. Google Translate rate-limitea o bloquea "
                  "estos rangos de forma agresiva.")
    except Exception:
        pass

    return ip


# ---------------------------------------------------------------------------
# 2. Endpoints de traduccion
# ---------------------------------------------------------------------------

def probar_google_gtx():
    nombre = "Google translate_a/single (client=gtx)"
    url = "https://translate.googleapis.com/translate_a/single"
    parametros = {"client": "gtx", "sl": "en", "tl": "es", "dt": "t", "q": TEXTO_PRUEBA}

    try:
        r = requests.get(url, params=parametros, headers=HEADERS, timeout=TIMEOUT)
        bloqueo, es_html, cuerpo = _analizar_respuesta(r)

        if r.status_code == 429 or bloqueo:
            _fail(f"{nombre} -> HTTP {r.status_code} BLOQUEO POR IP")
            print(f"  {_resumen_cuerpo(cuerpo)}")
            return False

        if r.status_code != 200:
            _fail(f"{nombre} -> HTTP {r.status_code}")
            return False

        datos = r.json()
        traduccion = "".join(f[0] for f in datos[0] if f and f[0])
        _ok(f"{nombre} -> '{traduccion}'")
        return True

    except Exception as error:
        _fail(f"{nombre} -> {type(error).__name__}: {error}")
        return False


def probar_google_clients5():
    """Endpoint alterno; historicamente menos rate-limitado que translate_a/single."""
    nombre = "Google clients5 (dict-chrome-ex)"
    url = "https://clients5.google.com/translate_a/t"
    parametros = {"client": "dict-chrome-ex", "sl": "en", "tl": "es", "q": TEXTO_PRUEBA}

    try:
        r = requests.get(url, params=parametros, headers=HEADERS, timeout=TIMEOUT)
        bloqueo, es_html, cuerpo = _analizar_respuesta(r)

        if r.status_code == 429 or bloqueo:
            _fail(f"{nombre} -> HTTP {r.status_code} BLOQUEO POR IP")
            return False

        if r.status_code != 200:
            _fail(f"{nombre} -> HTTP {r.status_code}")
            return False

        datos = r.json()
        if isinstance(datos, list) and datos:
            primero = datos[0]
            traduccion = primero[0] if isinstance(primero, list) else str(primero)
        else:
            traduccion = str(datos)

        _ok(f"{nombre} -> '{traduccion}'")
        return True

    except Exception as error:
        _fail(f"{nombre} -> {type(error).__name__}: {error}")
        return False


def probar_mymemory():
    nombre = "MyMemory (sin key)"
    url = "https://api.mymemory.translated.net/get"
    parametros = {"q": TEXTO_PRUEBA[:400], "langpair": "en|es"}

    email = os.getenv("MYMEMORY_EMAIL")
    if email:
        parametros["de"] = email

    try:
        r = requests.get(url, params=parametros, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            _fail(f"{nombre} -> HTTP {r.status_code}: {_resumen_cuerpo(r.text)}")
            return False

        datos = r.json()
        estado = datos.get("responseStatus")
        traduccion = (datos.get("responseData") or {}).get("translatedText", "")

        if str(estado) != "200" or not traduccion:
            _fail(f"{nombre} -> responseStatus={estado}: {_resumen_cuerpo(datos)}")
            return False

        _ok(f"{nombre} -> '{traduccion}'")
        if not email:
            _warn("  Sin MYMEMORY_EMAIL el limite anonimo es ~5000 chars/dia "
                  "por IP; con email sube a ~50000.")
        return True

    except Exception as error:
        _fail(f"{nombre} -> {type(error).__name__}: {error}")
        return False


def probar_lingva():
    nombre = "Lingva Translate"
    base = os.getenv("LINGVA_URL", "https://lingva.ml").rstrip("/")
    url = f"{base}/api/v1/en/es/{urllib.parse.quote(TEXTO_PRUEBA)}"

    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            _fail(f"{nombre} -> HTTP {r.status_code}")
            return False

        traduccion = r.json().get("translation", "")
        if not traduccion:
            _fail(f"{nombre} -> respuesta sin campo 'translation'")
            return False

        _ok(f"{nombre} -> '{traduccion}'")
        return True

    except Exception as error:
        _fail(f"{nombre} -> {type(error).__name__}: {error}")
        return False


def probar_libretranslate():
    nombre = "LibreTranslate"
    base = os.getenv("LIBRETRANSLATE_URL")
    if not base:
        _warn(f"{nombre} -> omitido (define LIBRETRANSLATE_URL para probarlo)")
        return None

    carga = {"q": TEXTO_PRUEBA, "source": "en", "target": "es", "format": "text"}
    if os.getenv("LIBRETRANSLATE_API_KEY"):
        carga["api_key"] = os.getenv("LIBRETRANSLATE_API_KEY")

    try:
        r = requests.post(f"{base.rstrip('/')}/translate", json=carga, timeout=TIMEOUT)
        if r.status_code != 200:
            _fail(f"{nombre} -> HTTP {r.status_code}: {_resumen_cuerpo(r.text)}")
            return False

        traduccion = r.json().get("translatedText", "")
        _ok(f"{nombre} -> '{traduccion}'")
        return True

    except Exception as error:
        _fail(f"{nombre} -> {type(error).__name__}: {error}")
        return False


def probar_deepl():
    nombre = "DeepL API Free"
    clave = os.getenv("DEEPL_API_KEY")
    if not clave:
        _warn(f"{nombre} -> omitido (define DEEPL_API_KEY para probarlo)")
        return None

    url = ("https://api-free.deepl.com/v2/translate"
           if clave.endswith(":fx")
           else "https://api.deepl.com/v2/translate")

    try:
        r = requests.post(
            url,
            headers={"Authorization": f"DeepL-Auth-Key {clave}"},
            data={"text": TEXTO_PRUEBA, "target_lang": "ES", "source_lang": "EN"},
            timeout=TIMEOUT
        )
        if r.status_code != 200:
            _fail(f"{nombre} -> HTTP {r.status_code}: {_resumen_cuerpo(r.text)}")
            return False

        traduccion = r.json()["translations"][0]["text"]
        _ok(f"{nombre} -> '{traduccion}'")
        return True

    except Exception as error:
        _fail(f"{nombre} -> {type(error).__name__}: {error}")
        return False


def probar_deep_translator():
    nombre = "deep_translator (libreria)"
    try:
        from deep_translator import GoogleTranslator
    except Exception as error:
        _fail(f"{nombre} -> no importable: {error}")
        return False

    try:
        resultado = GoogleTranslator(source="en", target="es").translate(TEXTO_PRUEBA)
        if not resultado:
            _fail(f"{nombre} -> devolvio vacio/None")
            return False
        if resultado.strip().lower() == TEXTO_PRUEBA.strip().lower():
            _fail(f"{nombre} -> devolvio el texto SIN traducir (endpoint roto)")
            return False
        _ok(f"{nombre} -> '{resultado}'")
        return True

    except Exception as error:
        _fail(f"{nombre} -> {type(error).__name__}: {error}")
        return False


# ---------------------------------------------------------------------------
# Veredicto
# ---------------------------------------------------------------------------

def main():
    print("DIAGNOSTICO DE TRADUCCION")
    print(f"Texto de prueba: '{TEXTO_PRUEBA}'")

    entorno()
    ip = conectividad()

    if ip is None:
        print("\nVEREDICTO: Capa D (red/TLS). El problema no es la traduccion.")
        return

    _seccion("2. PROVEEDORES DE TRADUCCION")
    resultados = {
        "google_gtx": probar_google_gtx(),
        "google_clients5": probar_google_clients5(),
        "deep_translator": probar_deep_translator(),
        "mymemory": probar_mymemory(),
        "lingva": probar_lingva(),
        "libretranslate": probar_libretranslate(),
        "deepl": probar_deepl(),
    }

    _seccion("3. VEREDICTO")
    google_ok = resultados["google_gtx"] or resultados["google_clients5"]
    alternos_ok = any(
        resultados[k] for k in ["mymemory", "lingva", "libretranslate", "deepl"]
    )

    if not google_ok and not alternos_ok:
        print("Capa A: bloqueo de egress o de IP contra todos los proveedores.")
        print("Accion: usar proxy/VPN, mover el bot a otra IP, o self-host "
              "LibreTranslate en la misma red.")
    elif not google_ok and alternos_ok:
        print("Capa A (parcial): Google bloquea tu IP; otros proveedores responden.")
        print("Accion: dejar de depender de Google. Usa DeepL o MyMemory como "
              "proveedor primario en traductor.py.")
    elif google_ok and not resultados["deep_translator"]:
        print("Capa B/C: el endpoint de Google responde, pero deep_translator no.")
        print("Accion: pip install -U deep-translator, o eliminar la libreria y "
              "usar solo HTTP directo (traductor.py ya lo hace).")
    else:
        print("Todos los proveedores clave responden.")
        print("Accion: la falla es intermitente (rate-limit por volumen) o esta "
              "en la Capa E (longitud). Revisa el log del bot y activa la cache.")

    print("\nResultados crudos:")
    print(json.dumps(resultados, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
