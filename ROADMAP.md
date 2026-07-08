# ROADMAP — solarview-alarms

Plan completo: `/home/obed/.claude/plans/quiero-implementar-un-m-dulo-glimmering-eagle.md`

Reglas de ejecución (para cada iteración del loop):
- Tomar la PRIMERA tarea sin marcar, saltando las Bloqueadas. UNA sola tarea por iteración.
- TDD: test primero, luego implementación. Nada se marca sin `pytest` y `ruff` verdes.
- Verde → commit atómico (`T0X: <descripción>`), marcar `[x]`, anotar gotchas en Notas.
- Falla tras 2 intentos → mover a Bloqueadas con diagnóstico y seguir con la siguiente.
- Sin tareas pendientes → cambiar Estado a COMPLETADO y terminar el loop.
- El token de la API y el webhook viven en `.env` (`static_token`, `webhook_discord`): cargarlos
  solo en runtime, NUNCA leer/imprimir el `.env` ni sus valores.
- API externa SIEMPRE vía alias `/monitoring/` (no `/api/`).

## Estado: EN PROGRESO

- [x] T01 Scaffolding: proyecto Django `config/` + settings (base/dev/prod con django-environ) + docker-compose (postgres:16, redis:7) + pyproject + pytest-django/ruff + healthcheck
- [ ] T02 `config/celery.py`: app Celery, colas (evaluation, notifications, sync), django-celery-beat
- [ ] T03 SONDEO APIs reales: scripts en `scripts/probe/` que consultan `/monitoring/` con `static_token`, graban responses reales como fixtures JSON en `integrations/solarview/tests/fixtures/`, documentan en Notas: formato de timestamps, cadencia real de weather/quoia por proyecto, códigos de `state` del inversor (¿distingue derating/aislamiento?), estructura de measurements-dc. GATE: lo aprendido ajusta params por defecto y puede mover tareas a Bloqueadas.
- [ ] T04 Cliente SolarView base: `integrations/solarview/client.py` (`/monitoring/`, envelope success/error, excepciones SolarViewAPIError/Timeout/AuthError, retries 429/5xx, timeout (5,30)) + tests con fixtures reales
- [ ] T05 Cliente SolarView: un método por endpoint + dataclasses en `schemas.py`
- [ ] T06 App `plants`: modelos Project/Inverter/MaintenanceWindow + migraciones + admin
- [ ] T07 `plants.sync_catalog`: task Celery de upsert por external_id (primer e2e)
- [ ] T08 Modelos `alarms`: AlarmRule, RuleConfig, Alarm (dedup_key + partial unique constraint), NonComputableInterval, EvaluationRun + migraciones + admin
- [ ] T09 Data migration seed: 20 AlarmRule con params COX (reglas 16 T_mod y 19 THD con enabled=False)
- [ ] T10 Modelos `notifications`: NotificationChannel (kind, env_key, discord_channel_id, min_severity), NotificationLog (target_channel_id snapshot, unique alarm+channel+event) + admin
- [ ] T11 Engine: `rules/base.py` (BaseRule/RuleOutcome/registry), `context.py` (EvaluationContext: cache perezoso de API, sentinel Unavailable, ventanas con lag `[now-persistence-lag, now-lag]`, is_solar_hours, in_maintenance, flag_active), `engine.py` (3 fases, upsert tri-estado, resolución inmediata sin histéresis)
- [ ] T12 Regla piloto 14 `weather_comm_lost` + ciclo completo upsert/dedup/auto-resolve + tests
- [ ] T13 Regla piloto 1 `project_no_generation` (ventanas/exclusiones/fases/not_computable) + tests
- [ ] T14 Canal Discord (embed por severidad, GET webhook→channel_id cacheado) + dispatcher + send_notification con retries + target_channel_id + tests
- [ ] T15 Reglas comunicación: 4 `inverter_comm_lost`, 8 `meter_comm_lost`
- [ ] T16 Reglas calidad datos: 11 `pr_inputs_missing` (sin T_mod), 12 `availability_inputs_missing`, 13 `data_frozen`, 15 `poa_invalid`
- [ ] T17 Reglas inversor/strings: 2 `inverter_unavailable`, 3 `inverter_derating`, 5 `string_zero_current`, 6 `string_low_current`, 7 `dc_isolation_low`
- [ ] T18 Reglas medidores: 9 `meter_no_increment`, 10 `meter_inverter_mismatch` (grupo hourly + escalamiento 3%/5%)
- [ ] T19 Reglas red/calidad: 17 `recloser_open`, 18 `power_factor_low` (+ stubs 16, 19 disabled)
- [ ] T20 `check_sla` (regla 20 `alarm_sla_breach`) + escalamiento
- [ ] T21 Hardening: locks Redis anti-solape, EvaluationRun como dashboard en admin, tuning colas

## Bloqueadas

(si una tarea falla 2 intentos o requiere decisión humana, va aquí con diagnóstico)

## Notas entre iteraciones

(gotchas que la siguiente iteración debe conocer: cadencias reales, formatos de timestamp, códigos de state, etc.)

- T01: venv en `.venv/` (python 3.12, sin uv). `pip install -e ".[dev]"`. Tests con `config.settings.test` (sqlite in-memory, CELERY_TASK_ALWAYS_EAGER). Settings leen `.env` vía django-environ: llaves `static_token`, `webhook_discord`, opcionales `SOLARVIEW_BASE_URL`, `DATABASE_URL`, `REDIS_URL`. docker-compose expone postgres 5432 y redis 6379.
