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
- [x] T02 `config/celery.py`: app Celery, colas (evaluation, notifications, sync), django-celery-beat
- [x] T03 SONDEO APIs reales: scripts en `scripts/probe/` que consultan `/monitoring/` con `static_token`, graban responses reales como fixtures JSON en `integrations/solarview/tests/fixtures/`, documentan en Notas: formato de timestamps, cadencia real de weather/quoia por proyecto, códigos de `state` del inversor (¿distingue derating/aislamiento?), estructura de measurements-dc. GATE: lo aprendido ajusta params por defecto y puede mover tareas a Bloqueadas.
- [x] T04 Cliente SolarView base: `integrations/solarview/client.py` (`/monitoring/`, envelope success/error, excepciones SolarViewAPIError/Timeout/AuthError, retries 429/5xx, timeout (5,30)) + tests con fixtures reales
- [x] T05 Cliente SolarView: un método por endpoint + dataclasses en `schemas.py`
- [x] T06 App `plants`: modelos Project/Inverter/MaintenanceWindow + migraciones + admin
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
- [ ] T18 Reglas medidores: 9 `meter_no_increment`, 10 `meter_inverter_mismatch` (grupo hourly + escalamiento 3%/5%). ⚠️ quoia roto server-side (ver Notas T03): si sigue roto al llegar aquí, mover 8/9/10 a Bloqueadas.
- [ ] T19 Reglas red/calidad: 17 `recloser_open`, 18 `power_factor_low` (+ stubs 16, 19 disabled)
- [ ] T20 `check_sla` (regla 20 `alarm_sla_breach`) + escalamiento
- [ ] T21 Hardening: locks Redis anti-solape, EvaluationRun como dashboard en admin, tuning colas

## Bloqueadas

(si una tarea falla 2 intentos o requiere decisión humana, va aquí con diagnóstico)

## Notas entre iteraciones

(gotchas que la siguiente iteración debe conocer: cadencias reales, formatos de timestamp, códigos de state, etc.)

- T06: `MaintenanceWindow.objects.active_at(project, at, inverter=None)`: sin inverter devuelve SOLO ventanas de proyecto completo; con inverter devuelve las suyas + las de proyecto. Semántica pensada para `ctx.in_maintenance()`. Migraciones generadas con `DJANGO_SETTINGS_MODULE=config.settings.test` (no requiere postgres corriendo).
- T05: timestamps se parsean a `datetime` NAIVE hora local (America/Bogota) en `schemas.parse_ts` — formatos `%Y-%m-%d %H:%M:%S`, `%Y-%m-%d %H:%M`, `%Y-%m-%d`. `TimeSeries = dict[datetime, float|None]` (None = inversor sin reporte, común de noche). Métodos tipados: list_projects, project_inverters, project_power, project_weather, relay_now, relay_historical, quoia_history, generation, measurements_dc, project_measurement, availability_detail. Usar `weather.irradiation_poa` para POA.
- T04: `SolarViewClient.get()` devuelve `results` si hay envelope, o el body crudo si no (p.ej. `/generation/` NO usa envelope). 404 con `success=false` → `SolarViewNotAssociated` (equipo no existe en el proyecto, no reintentar). 401/403 → `SolarViewAuthError`. Retries agotados → `SolarViewAPIError` (urllib3 lanza RetryError, mapeado). `from_settings()` importa Django lazy — el paquete sigue siendo usable sin Django. Settings ahora leen `SOLARSOLARVIEW_BASE_URL` con fallback a `SOLARVIEW_BASE_URL`.
- T03 (SONDEO, proyectos 121 y 146; 77 proyectos visibles con el token):
  - **Auth**: `Authorization: Token <static_token>`. Llave real del .env para la URL: `SOLARSOLARVIEW_BASE_URL` (sin esquema — anteponer `https://`).
  - **`state` del inversor es STRING legible** ("Grid-connected"), NO código numérico como decía la doc. Solo se observó ese valor → los códigos de derating/aislamiento (reglas 3 y 7) siguen sin conocerse; descubrirlos cuando haya un inversor en falla o preguntar al equipo del backend.
  - **Weather real** tiene MÁS llaves que la doc: `irradiation`, `irradiation_POA`, `temperature`, `temperature_POA`, `wind_speed`, `wind_direction` (con `unit` por variable). Usar `irradiation_POA` para las reglas con POA. Cadencia ~1 min (mediana 1.0m, max 5m). Timestamps con segundos: `2026-07-08 00:00:10`.
  - **`/power/?total_power=1`**: `power` cada 5 min (`YYYY-MM-DD HH:MM`, SIN segundos), `irradiance` cada ~1 min. Ambos arrancan en 00:00 del día.
  - **`/inverter/` live**: `time` con segundos, los N inversores comparten el mismo timestamp (lote de escritura).
  - **`/relay/` real** trae más campos que la doc (`i_n`, `u_r/u_s/u_t`) y **`active` puede ser `null`** → la regla 17 debe tratar `active=null` como not_computable. Se vio `u_a/u_b/u_c=0` con `kw=1` y `pf=0` — validar semántica antes de usar tensiones.
  - **`/measurements-dc/`**: dict por `dev_name` del inversor (NO por id) → variables `cs1..csN` con huecos (cs5 ausente) → `{ts: valor}` cada 5 min; valores nocturnos 0.0 y puede haber `None`.
  - **`/measurement/`**: `{variable: {dev_name: {ts: valor}}}` cada 5 min, con `None` cuando el inversor no reporta (noche).
  - **`/availability_detail/`**: inversores por `dev_name`; cada uno trae `availability`, `available`, `not_available` y solo los `pvN` con strings.
  - **⚠️ QUOIA ROTO**: `/quoia_measurements_history/` devuelve 404/500 en TODOS los proyectos sondeados (25). Errores del backend: `ProjectInfo matching query does not exist` y `cannot access local variable 'updated_node'`. Las reglas de medidor frontera (8, 9, 10) no tienen fuente de datos hasta que el backend lo arregle → probable Bloqueadas en T18. Alternativa a explorar: `frontier_generation` de `/project_summary/`.
  - Los 404 de negocio ("no existe estación meteorológica", "Relay not found") vienen con envelope `success=false` y HTTP 404 → el cliente debe mapearlos a una excepción distinta (`SolarViewNotAssociated`), porque "el proyecto no tiene ese equipo" NO es un error transitorio: las reglas de ese equipo deben saltarse para ese proyecto.
  - Proyectos sin lat/lon en el list (no visto aún) y `weather_plant_code` casi siempre `None` — solo 4/77 lo tienen. La mayoría de proyectos NO tiene estación meteo propia → regla 14 aplicará a pocos proyectos; para el resto, POA viene de `/power/` (campo `irradiance`).
- T02: task_routes por módulo (`apps.alarms.tasks.*`→evaluation, `apps.notifications.tasks.*`→notifications, `apps.plants.tasks.*`→sync) — los tasks futuros DEBEN vivir en esos módulos para caer en su cola. Soft time limit global 240s. Beat = DatabaseScheduler (schedules editables en admin; los defaults se seedearán cuando existan los tasks).
- T01: venv en `.venv/` (python 3.12, sin uv). `pip install -e ".[dev]"`. Tests con `config.settings.test` (sqlite in-memory, CELERY_TASK_ALWAYS_EAGER). Settings leen `.env` vía django-environ: llaves `static_token`, `webhook_discord`, opcionales `SOLARVIEW_BASE_URL`, `DATABASE_URL`, `REDIS_URL`. docker-compose expone postgres 5432 y redis 6379.
