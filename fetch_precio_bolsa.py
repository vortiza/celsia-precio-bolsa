"""
Actualiza data/precio_bolsa_historico.json con el histórico horario del
Precio de Bolsa Nacional publicado por XM (API pública, sin usuario/clave).

Uso:
    python fetch_precio_bolsa.py

Pensado para correr semanalmente desde GitHub Actions (ver
.github/workflows/actualizar_precio_bolsa.yml), pero también se puede
ejecutar a mano desde cualquier computador con Python 3 y `requests`
instalado (pip install requests).

Comportamiento:
- Si data/precio_bolsa_historico.json no existe todavía, hace una carga
  inicial de los últimos BACKFILL_DAYS días (por defecto 400).
- Si ya existe, solo vuelve a traer los últimos OVERLAP_DAYS días
  (por defecto 14) para capturar datos que XM haya publicado o corregido
  tarde, y los fusiona con lo que ya había, sin duplicar.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

import requests

API_URL = "https://servapibi.xm.com.co/hourly"
OUTPUT_PATH = os.path.join("data", "precio_bolsa_historico.json")
BACKFILL_DAYS = 400
OVERLAP_DAYS = 14
MAX_DAYS_PER_CALL = 30  # límite de la API de XM para datos horarios


def chunk_dates(start: date, end: date, max_days: int):
    chunks = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def fetch_range(start: date, end: date):
    body = {
        "MetricId": "PrecBolsNaci",
        "StartDate": start.isoformat(),
        "EndDate": end.isoformat(),
        "Entity": "Sistema",
        "Filter": [],
    }
    resp = requests.post(API_URL, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()


def parse_items(payload):
    records = []
    for item in payload.get("Items", []):
        date_str = str(item.get("Date", ""))[:10]
        if not date_str:
            continue
        for entity in item.get("HourlyEntities", []):
            values = entity.get("Values", entity)
            for h in range(1, 25):
                key = f"Hour{h:02d}"
                val = values.get(key)
                if val is None:
                    continue
                try:
                    price = float(val)
                except (TypeError, ValueError):
                    continue
                records.append({"date": date_str, "hour": h - 1, "price": price})
    return records


def load_existing():
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"generated_at": None, "records": []}


def main():
    existing = load_existing()
    is_first_run = not existing["records"]

    today = date.today()
    if is_first_run:
        start = today - timedelta(days=BACKFILL_DAYS)
        print(f"Primera ejecución: trayendo los últimos {BACKFILL_DAYS} días.")
    else:
        start = today - timedelta(days=OVERLAP_DAYS)
        print(f"Actualización incremental: trayendo los últimos {OVERLAP_DAYS} días.")

    index = {(r["date"], r["hour"]): r for r in existing["records"]}
    added, updated = 0, 0

    for chunk_start, chunk_end in chunk_dates(start, today, MAX_DAYS_PER_CALL):
        print(f"  Consultando {chunk_start} a {chunk_end} ...")
        try:
            payload = fetch_range(chunk_start, chunk_end)
        except requests.RequestException as exc:
            print(f"  ERROR consultando ese rango: {exc}", file=sys.stderr)
            continue
        for rec in parse_items(payload):
            key = (rec["date"], rec["hour"])
            if key not in index:
                index[key] = rec
                added += 1
            elif index[key]["price"] != rec["price"]:
                index[key]["price"] = rec["price"]
                updated += 1

    existing["records"] = sorted(index.values(), key=lambda r: (r["date"], r["hour"]))
    existing["generated_at"] = datetime.utcnow().isoformat() + "Z"

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"Listo. {added} registros nuevos, {updated} corregidos. "
          f"Total en el histórico: {len(existing['records'])}.")


if __name__ == "__main__":
    main()
