# solarview-alarms

Sistema de alarmas para plantas solares SolarView: lee datos de la API de
monitoreo (vía el alias `/monitoring/`), evalúa las alarmas de Fase 1 (19 de
20 activas; solo THD deshabilitada) cada 5 minutos con Celery, persiste
alarmas deduplicadas en PostgreSQL y notifica a Discord con trazabilidad del
canal de destino.

## Arquitectura

```
integrations/solarview/   Cliente HTTP tipado (auth Token, retries, excepciones)
apps/plants/              Cache local de proyectos/inversores + ventanas de mantenimiento
apps/alarms/              Catálogo de reglas, engine tri-estado, alarmas, bitácora de runs
apps/notifications/       Canal Discord + dispatcher idempotente + log con channel_id
config/                   Settings (django-environ), Celery (colas + beat)
```

Documentación:
- `docs/DISENO.md` — diseño completo: modelos campo por campo, motor de
  evaluación (contrato de regla, EvaluationContext, semántica tri-estado),
  mapa alarma→endpoints y decisiones de arquitectura.
- `ROADMAP.md` — historia de implementación: tareas, gotchas de la API real y
  pendientes que requieren acción del backend (sección Bloqueadas).
- Cada regla documenta su lógica y exclusiones en su docstring
  (`apps/alarms/rules/`); las descripciones del catálogo se ven en el admin.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d          # postgres:16 + redis:7
cp .env.example .env          # y llenar las llaves (ver tabla abajo)
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
```

Llaves del `.env` (nunca commitear):

| Llave | Contenido |
|---|---|
| `SOLARSOLARVIEW_BASE_URL` | Host de la API (sin esquema; se antepone https://) |
| `static_token` | Token estático de la API SolarView |
| `webhook_discord` | URL del webhook de Discord |
| `DATABASE_URL` | opcional, default postgres local |
| `REDIS_URL` | opcional, default redis local |

## Operación

```bash
# worker + beat (los schedules por defecto ya están seedeados por migración)
.venv/bin/celery -A config worker -B -l info

# admin: UI operativa (alarmas, umbrales por proyecto, canales, schedules, runs)
.venv/bin/python manage.py runserver
```

- Los schedules (evaluación cada 5 min, hourly, SLA cada 10 min, sync horario)
  se editan en admin → Periodic tasks.
- El canal `ops-discord` nace **deshabilitado**: encenderlo en admin →
  Notification channels cuando el webhook esté configurado.
- Umbrales COX: defaults en el catálogo (admin → Alarm rules); override por
  proyecto en Rule configs.
- Mantenimientos programados (excluyen alarmas): admin → Maintenance windows.

## Tests

```bash
.venv/bin/pytest && .venv/bin/ruff check .
```

## Estado conocido (2026-07-08)

- Quoia (medidor de frontera) FUNCIONA en 26/77 proyectos vía
  `/quoia_measurements_history/` **sin parámetros** (T28: cualquier query param
  dispara un 500 del backend; el payload es energía por intervalo ~15 min, no
  contador). Las reglas 8/9/10 operan con datos reales ahí. Para el resto, el
  live `/quoia_measurements/` (roto para mediciones) actúa de oráculo de
  existencia (T29): 45 proyectos **sin medidor** → las reglas no aplican (`[]`);
  6 con medidor pero sin datos → `not_computable` sin ruido. Ver Bloqueadas en
  `ROADMAP.md`.
- `state` del inversor: strings legibles con formato `"Modo: detalle"`.
  Observados "Grid-connected" y "Standby: insulation resistance detecting"
  (auto-test rutinario, NO falla — la regla 7 exige calificador
  low/fault/abnormal). El vocabulario de DERATING (regla 3) sigue sin
  conocerse: keywords tentativos.
- La API entrega lat/lon invertidas en al menos un proyecto (151, DEPRECATED,
  excluido con `monitoring_enabled=False`): `is_solar_hours` cae al horario
  fijo cuando astral explota por coordenadas inválidas.
- `relay.kw` llega en escalas inconsistentes según el proyecto (se sospecha W
  en vez de kW): `rules/relay_normalize.py` resuelve la unidad por
  plausibilidad física contra la capacidad instalada; ambigüedad irresoluble
  → `not_computable`. Pendiente confirmación del backend.
- Regla 16 (T_mod = `temperature_POA`) activa desde la migración 0004; solo
  la 19 (THD) sigue deshabilitada — la API no expone THD.
