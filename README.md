# solarview-alarms

Sistema de alarmas para plantas solares SolarView: lee datos de la API de
monitoreo (vía el alias `/monitoring/`), evalúa las 20 alarmas de Fase 1 cada
5 minutos con Celery, persiste alarmas deduplicadas en PostgreSQL y notifica a
Discord con trazabilidad del canal de destino.

## Arquitectura

```
integrations/solarview/   Cliente HTTP tipado (auth Token, retries, excepciones)
apps/plants/              Cache local de proyectos/inversores + ventanas de mantenimiento
apps/alarms/              Catálogo de reglas, engine tri-estado, alarmas, bitácora de runs
apps/notifications/       Canal Discord + dispatcher idempotente + log con channel_id
config/                   Settings (django-environ), Celery (colas + beat)
```

Documentación viva del proceso: `ROADMAP.md` (tareas, decisiones y gotchas).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d          # postgres:16 + redis:7
cp .env.example .env          # o crear .env con las llaves de abajo
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

- Quoia (medidor de frontera) devuelve 500 server-side en todos los proyectos:
  las reglas 8/9/10 viven en `not_computable` (sin ruido) hasta que el backend
  lo arregle. Ver Bloqueadas en `ROADMAP.md`.
- `state` del inversor solo se ha observado como "Grid-connected": los keywords
  de derating/aislamiento (reglas 3/7) son tentativos.
- Reglas 16 (T_mod) y 19 (THD) deshabilitadas por diseño.
