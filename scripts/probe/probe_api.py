"""Sondeo de la API SolarView real.

Consulta los endpoints via /solarview/ con el static_token del .env, graba los
responses como fixtures JSON y reporta hallazgos (cadencias, formatos, codigos).

Uso:
    .venv/bin/python scripts/probe/probe_api.py [--project-id N]

Nunca imprime el token ni el contenido del .env.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import environ
import requests

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = BASE_DIR / "integrations" / "solarview" / "tests" / "fixtures"

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

AUTH_STYLES = {
    "token": lambda t: {"Authorization": f"Token {t}"},
    "bearer": lambda t: {"Authorization": f"Bearer {t}"},
    "static-token": lambda t: {"static-token": t},
    "x-api-key": lambda t: {"X-API-KEY": t},
}


def detect_auth(base_url: str, token: str) -> dict | None:
    """Prueba auth contra el inventario canónico y devuelve el estilo válido."""
    for name, builder in AUTH_STYLES.items():
        try:
            resp = requests.get(
                f"{base_url}/solarview/config/company-projects/",
                headers=builder(token),
                timeout=(5, 30),
            )
        except requests.RequestException as exc:
            print(f"  auth={name}: error de red: {type(exc).__name__}")
            continue
        print(f"  auth={name}: HTTP {resp.status_code}")
        if resp.status_code == 200:
            print(f"OK: la API acepta el estilo de auth '{name}'")
            return builder(token)
    return None


def save_fixture(name: str, payload: object) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    print(f"  fixture: {path.relative_to(BASE_DIR)}")


def fetch(headers: dict, base_url: str, path: str, name: str, params: dict | None = None):
    url = f"{base_url}/solarview/{path}"
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=(5, 60))
    except requests.RequestException as exc:
        print(f"[{name}] {path} -> error de red: {type(exc).__name__}: {exc}")
        return None
    print(f"[{name}] GET /solarview/{path} params={params} -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"  body (truncado): {resp.text[:300]}")
        return None
    try:
        body = resp.json()
    except ValueError:
        print(f"  respuesta no-JSON (truncada): {resp.text[:200]}")
        return None
    save_fixture(name, body)
    return body


def analyze_cadence(name: str, timestamps: list[str]) -> None:
    """Imprime la cadencia (deltas entre timestamps consecutivos) de una serie."""
    parsed = []
    for ts in sorted(timestamps):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed.append(datetime.strptime(ts, fmt))
                break
            except ValueError:
                continue
    if len(parsed) < 2:
        print(f"  [{name}] cadencia: <2 puntos parseables de {len(timestamps)}")
        return
    deltas = sorted((b - a).total_seconds() / 60 for a, b in zip(parsed, parsed[1:], strict=False))
    mid = deltas[len(deltas) // 2]
    print(
        f"  [{name}] cadencia: n={len(parsed)} min={deltas[0]:.1f}m "
        f"mediana={mid:.1f}m max={deltas[-1]:.1f}m | ejemplo ts: {timestamps[0]!r}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int, default=None)
    args = parser.parse_args()

    base_url = ""
    for key in (
        "SOLARVIEW_BASE_URL",
        "SOLARSOLARVIEW_BASE_URL",
        "solarview_base_url",
        "base_url",
        "BASE_URL",
        "api_url",
    ):
        base_url = env(key, default="").rstrip("/")
        if base_url:
            print(f"Base URL tomada de la llave: {key}")
            break
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    token = env("static_token", default="")
    if not base_url or not token:
        pairs = [("SOLARVIEW_BASE_URL", base_url), ("static_token", token)]
        missing = [k for k, v in pairs if not v]
        print(f"FALTAN llaves en .env: {missing}")
        return 1

    print(f"Base URL: {base_url}")
    print("Detectando estilo de auth...")
    headers = detect_auth(base_url, token)
    if headers is None:
        print("FALLO: ningun estilo de auth devolvio 200")
        return 2

    projects = fetch(headers, base_url, "config/company-projects/", "project_list")
    if not projects:
        return 3

    results = projects.get("results", projects)
    data = results.get("data", results) if isinstance(results, dict) else results
    if not data:
        print("Sin proyectos visibles para este token")
        return 4
    project = (
        data[0] if args.project_id is None else next(p for p in data if p["id"] == args.project_id)
    )
    pid = project["id"]
    print(f"\nProyecto de sondeo: id={pid} name={project.get('name')!r}\n")

    today = datetime.now().strftime("%Y-%m-%d")

    fetch(headers, base_url, f"config/project-detail/{pid}/", "project_detail_basic")
    inv = fetch(
        headers, base_url, "measurements/inverters-list/", "inverters_live", {"project_id": pid}
    )
    fetch(
        headers,
        base_url,
        "measurements/power/",
        "power_today",
        {"project_id": pid, "total_power": 1},
    )
    weather = fetch(
        headers,
        base_url,
        "measurements/weather/",
        "weather_today",
        {
            "project_id": pid,
            "date_from": f"{today} 00:00:00-05:00",
            "date_to": f"{today} 23:59:59-05:00",
        },
    )
    fetch(headers, base_url, "config/recloser/", "relay_now", {"project_id": pid})
    quoia = fetch(
        headers,
        base_url,
        "measurements/border/historical/",
        "quoia_history_today",
        {
            "project_id": pid,
            "init_date": f"{today}T00:00:00-05:00",
            "end_date": f"{today}T23:59:59-05:00",
        },
    )
    fetch(
        headers,
        base_url,
        "measurements/generation/",
        "generation_today",
        {"project_id": pid, "start_date": today, "end_date": today},
    )
    fetch(
        headers,
        base_url,
        "measurements/dc/",
        "measurements_dc_cs",
        {"project_id": pid, "variable": "cs"},
    )
    fetch(
        headers,
        base_url,
        "measurements/ac/",
        "measurement_vp1",
        {"project_id": pid, "variable": "vp1"},
    )
    fetch(
        headers, base_url, "kpis/availability/detail/", "availability_detail", {"project_id": pid}
    )

    print("\n--- Analisis de cadencias ---")
    if inv:
        times = [i["time"] for i in inv.get("results", []) if i.get("time")]
        print(f"  [inverters] {len(times)} inversores, ultimo dato ej: {times[:2]}")
    if weather:
        irr = (weather.get("results") or {}).get("irradiation") or {}
        analyze_cadence("weather.irradiation", list(irr.keys()))
    if quoia:
        analyze_cadence("quoia", list((quoia.get("results") or {}).keys()))

    print("\nSondeo completo. Fixtures en integrations/solarview/tests/fixtures/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
