"""
Actualiza data/curva_oferta_historico.json con la curva de oferta (precio vs.
cantidad acumulada, por recurso) reconstruida a partir de la API pública de XM.

A diferencia de fetch_precio_bolsa.py (que usa una métrica ya verificada,
"PrecBolsNaci"), esta curva no viene lista en un solo indicador: hay que
combinar el PRECIO de oferta de cada recurso con la CANTIDAD despachada de
cada recurso, y luego ordenar de menor a mayor precio para construir la curva
acumulada (igual a las columnas Inicio_MW / Fin_MW del archivo que subiste a
mano).

Como no fue posible confirmar de antemano los nombres exactos de esas dos
métricas en el catálogo de XM, este script las BUSCA por su cuenta en
"ListadoMetricas" cada vez que corre, en lugar de asumir un nombre fijo que
podría estar mal y fallar en silencio. Si no logra identificarlas con
confianza, se detiene y explica qué encontró, en vez de guardar datos
incorrectos.

Recomendación: la primera vez, corre este script (o el workflow) a mano y
revisa el mensaje que imprime en la consola / en los logs de GitHub Actions,
para confirmar que sí encontró las métricas correctas antes de dejarlo
corriendo solo cada semana.

Por tamaño, este script solo trae los últimos OVERLAP_DAYS días en cada
corrida (no hace una carga masiva de meses hacia atrás como el de precios,
porque el volumen de datos por recurso es mucho mayor).
"""

import json
import os
import sys
from datetime import date, timedelta, datetime

import requests

LISTS_URL = "https://servapibi.xm.com.co/Lists"
HOURLY_URL = "https://servapibi.xm.com.co/hourly"
OUTPUT_PATH = os.path.join("data", "curva_oferta_historico.json")
OVERLAP_DAYS = 7

# Palabras que debe contener el nombre de la métrica de PRECIO por recurso
PRICE_KEYWORDS = ["precio", "oferta"]
PRICE_EXCLUDE = ["bolsa", "contrato", "promedio", "escasez"]

# Palabras que debe contener el nombre de la métrica de CANTIDAD por recurso
QTY_KEYWORDS = ["generacion", "ideal"]
QTY_EXCLUDE = ["real", "seguridad", "firme", "restriccion"]


def normalize(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode()
    return s.lower()


def fetch_catalog():
    resp = requests.post(LISTS_URL, json={"MetricId": "ListadoMetricas"}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    rows = []
    for item in data.get("Items", []):
        for row in item.get("ListEntities", []):
            rows.append(row)
    return rows


def find_metric(catalog, keywords, exclude, entity_wanted="Recurso"):
    candidates = []
    for row in catalog:
        entity = row.get("Entity", "")
        if entity != entity_wanted:
            continue
        name_fields = " ".join(str(v) for v in row.values())
        norm = normalize(name_fields)
        if all(k in norm for k in keywords) and not any(e in norm for e in exclude):
            candidates.append(row)
    return candidates


def chunk_dates(start, end, max_days):
    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def fetch_by_resource(metric_id, start, end):
    body = {
        "MetricId": metric_id,
        "StartDate": start.isoformat(),
        "EndDate": end.isoformat(),
        "Entity": "Recurso",
        "Filter": [],
    }
    resp = requests.post(HOURLY_URL, json=body, timeout=120)
    resp.raise_for_status()
    payload = resp.json()
    # date -> resource_id -> {hour: value}
    out = {}
    for item in payload.get("Items", []):
        date_str = str(item.get("Date", ""))[:10]
        if not date_str:
            continue
        for ent in item.get("HourlyEntities", []):
            values = ent.get("Values", ent)
            resource_id = ent.get("Id") or ent.get("Values_Id") or "?"
            for h in range(1, 25):
                key = f"Hour{h:02d}"
                v = values.get(key)
                if v is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                out.setdefault(date_str, {}).setdefault(resource_id, {})[h - 1] = v
    return out


def build_curves(price_data, qty_data):
    """price_data / qty_data: date -> resource -> {hour: value}. Devuelve
    date -> hour -> [[precio, mw_acumulado_inicio, mw_acumulado_fin], ...]"""
    curves = {}
    dates = set(price_data) & set(qty_data)
    for d in dates:
        resources = set(price_data[d]) & set(qty_data[d])
        for h in range(24):
            rows = []
            for r in resources:
                p = price_data[d][r].get(h)
                q = qty_data[d][r].get(h)
                if p is None or q is None or q <= 0:
                    continue
                rows.append((p, q))
            if not rows:
                continue
            rows.sort(key=lambda x: x[0])
            cum = 0.0
            curve = []
            for p, q in rows:
                inicio = cum
                cum += q
                curve.append([round(p, 3), round(inicio, 2), round(cum, 2)])
            curves.setdefault(d, {})[str(h)] = curve
    return curves


def load_existing():
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"generated_at": None, "price_metric": None, "qty_metric": None, "curves": {}}


def main():
    print("Buscando en el catálogo de XM las métricas de precio y cantidad por recurso...")
    catalog = fetch_catalog()
    price_candidates = find_metric(catalog, PRICE_KEYWORDS, PRICE_EXCLUDE)
    qty_candidates = find_metric(catalog, QTY_KEYWORDS, QTY_EXCLUDE)

    print(f"Candidatos de PRECIO por recurso encontrados: {len(price_candidates)}")
    for c in price_candidates[:10]:
        print("  ", c)
    print(f"Candidatos de CANTIDAD (generación ideal) por recurso encontrados: {len(qty_candidates)}")
    for c in qty_candidates[:10]:
        print("  ", c)

    if not price_candidates or not qty_candidates:
        print("ERROR: no se pudo identificar con confianza una o ambas métricas.", file=sys.stderr)
        print("No se va a guardar nada para evitar datos incorrectos.", file=sys.stderr)
        print("Revisa manualmente el catálogo completo (imprímelo sin filtrar) y ajusta", file=sys.stderr)
        print("PRICE_KEYWORDS / QTY_KEYWORDS en este script.", file=sys.stderr)
        sys.exit(1)

    price_metric = price_candidates[0]["MetricId"]
    qty_metric = qty_candidates[0]["MetricId"]
    print(f"Usando MetricId de precio: {price_metric}")
    print(f"Usando MetricId de cantidad: {qty_metric}")
    if len(price_candidates) > 1 or len(qty_candidates) > 1:
        print("AVISO: había más de un candidato — se tomó el primero. Verifica que sea el correcto.")

    today = date.today()
    start = today - timedelta(days=OVERLAP_DAYS)

    price_data, qty_data = {}, {}
    for s, e in chunk_dates(start, today, 5):  # lotes chicos: hay muchos recursos
        print(f"  Precio por recurso {s} a {e} ...")
        price_data.update(fetch_by_resource(price_metric, s, e))
        print(f"  Cantidad por recurso {s} a {e} ...")
        qty_data.update(fetch_by_resource(qty_metric, s, e))

    new_curves = build_curves(price_data, qty_data)

    existing = load_existing()
    existing["price_metric"] = price_metric
    existing["qty_metric"] = qty_metric
    existing.setdefault("curves", {})
    added_days = 0
    for d, hours in new_curves.items():
        if d not in existing["curves"]:
            added_days += 1
        existing["curves"][d] = hours
    existing["generated_at"] = datetime.utcnow().isoformat() + "Z"

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"Listo. {added_days} días nuevos/actualizados. Total de días guardados: {len(existing['curves'])}.")


if __name__ == "__main__":
    main()
