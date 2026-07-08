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

## Estado: COMPLETADO (2026-07-08) — quedan 2 items en Bloqueadas que requieren acción del backend

- [x] T01 Scaffolding: proyecto Django `config/` + settings (base/dev/prod con django-environ) + docker-compose (postgres:16, redis:7) + pyproject + pytest-django/ruff + healthcheck
- [x] T02 `config/celery.py`: app Celery, colas (evaluation, notifications, sync), django-celery-beat
- [x] T03 SONDEO APIs reales: scripts en `scripts/probe/` que consultan `/monitoring/` con `static_token`, graban responses reales como fixtures JSON en `integrations/solarview/tests/fixtures/`, documentan en Notas: formato de timestamps, cadencia real de weather/quoia por proyecto, códigos de `state` del inversor (¿distingue derating/aislamiento?), estructura de measurements-dc. GATE: lo aprendido ajusta params por defecto y puede mover tareas a Bloqueadas.
- [x] T04 Cliente SolarView base: `integrations/solarview/client.py` (`/monitoring/`, envelope success/error, excepciones SolarViewAPIError/Timeout/AuthError, retries 429/5xx, timeout (5,30)) + tests con fixtures reales
- [x] T05 Cliente SolarView: un método por endpoint + dataclasses en `schemas.py`
- [x] T06 App `plants`: modelos Project/Inverter/MaintenanceWindow + migraciones + admin
- [x] T07 `plants.sync_catalog`: task Celery de upsert por external_id (primer e2e)
- [x] T08 Modelos `alarms`: AlarmRule, RuleConfig, Alarm (dedup_key + partial unique constraint), NonComputableInterval, EvaluationRun + migraciones + admin
- [x] T09 Data migration seed: 20 AlarmRule con params COX (reglas 16 T_mod y 19 THD con enabled=False)
- [x] T10 Modelos `notifications`: NotificationChannel (kind, env_key, discord_channel_id, min_severity), NotificationLog (target_channel_id snapshot, unique alarm+channel+event) + admin
- [x] T11 Engine: `rules/base.py` (BaseRule/RuleOutcome/registry), `context.py` (EvaluationContext: cache perezoso de API, sentinel Unavailable, ventanas con lag `[now-persistence-lag, now-lag]`, is_solar_hours, in_maintenance, flag_active), `engine.py` (3 fases, upsert tri-estado, resolución inmediata sin histéresis)
- [x] T12 Regla piloto 14 `weather_comm_lost` + ciclo completo upsert/dedup/auto-resolve + tests
- [x] T13 Regla piloto 1 `project_no_generation` (ventanas/exclusiones/fases/not_computable) + tests
- [x] T14 Canal Discord (embed por severidad, GET webhook→channel_id cacheado) + dispatcher + send_notification con retries + target_channel_id + tests
- [x] T15 Reglas comunicación: 4 `inverter_comm_lost`, 8 `meter_comm_lost`
- [x] T16 Reglas calidad datos: 11 `pr_inputs_missing` (sin T_mod), 12 `availability_inputs_missing`, 13 `data_frozen`, 15 `poa_invalid`
- [x] T17 Reglas inversor/strings: 2 `inverter_unavailable`, 3 `inverter_derating`, 5 `string_zero_current`, 6 `string_low_current`, 7 `dc_isolation_low`
- [x] T18 Reglas medidores: 9 `meter_no_increment`, 10 `meter_inverter_mismatch` (grupo hourly + escalamiento 3%/5%). Implementadas contra la forma documentada del payload; quoia sigue 500 (re-verificado en 3 proyectos) → validación real pendiente en Bloqueadas.
- [x] T19 Reglas red/calidad: 17 `recloser_open`, 18 `power_factor_low` (+ stubs 16, 19 disabled)
- [x] T20 `check_sla` (regla 20 `alarm_sla_breach`) + escalamiento
- [x] T21 Hardening: schedules beat por defecto (migración), canal ops-discord seed (disabled), dashboard EvaluationRun en admin, README de operación, smoke e2e del engine completo contra API real

## Bloqueadas

(si una tarea falla 2 intentos o requiere decisión humana, va aquí con diagnóstico)

- **Validar reglas 8/9/10 contra quoia real**: `/quoia_measurements_history/` devuelve 500 en TODOS los proyectos (bugs backend: `ProjectInfo matching query does not exist` y `cannot access local variable 'updated_node'`). Las 3 reglas están implementadas contra la doc y viven en not_computable (sin ruido) mientras tanto. Acción humana: reportar al equipo del backend; al arreglarse, sondear payload real y ajustar `_frontier_delta_kwh` si la forma difiere.
- **Códigos de `state` del inversor para reglas 3 y 7**: solo se ha observado "Grid-connected". DERATING_KEYWORDS/ISOLATION_KEYWORDS son tentativos. Acción humana: pedir al backend el vocabulario completo de estados (¿expone derating/aislamiento?) o capturar states durante una falla real.

## Post-COMPLETADO

- [x] T22 (2026-07-08): T_mod definido por el usuario = `temperature_POA` (temperatura del panel). Regla 16 `tmod_invalid` implementada (missing/frozen/out_of_range/incoherent_vs_ambient, solo horario solar, sin estación no aplica) + habilitada por migración 0004; regla 11 ahora exige T_mod cuando el proyecto tiene estación. 19/20 reglas activas (solo THD disabled).

## Notas entre iteraciones

(gotchas que la siguiente iteración debe conocer: cadencias reales, formatos de timestamp, códigos de state, etc.)

- T21: smoke e2e real: proyecto 146 SUCCESS 25s (muchas requests; aceptable con fan-out paralelo, vigilar con 77 proyectos), 121 SUCCESS 4.5s y abrió alarma real `poa_invalid` con cascada de exclusiones correcta (reglas dependientes de POA → not_computable). Canal ops-discord seed DISABLED — encenderlo en admin al salir a producción. Schedules beat por migración (editables en admin).
- T20: `check_sla` reusa `_process_outcome` del engine (upsert/dedup/notify idénticos). Dedup del breach: `alarm_sla_breach:{ext_id}:alarm:{source_id}` — el source_id se recupera del dedup_key al resolver. SLA solo sobre ACTIVE (ack detiene el reloj). Breaches excluidos del scan (sin recursión). Escala a critical al superar sla × escalate_after_multiplier.
- T19: `validate_registry()` ahora vacío (test lo garantiza): las 19 reglas de engine tienen clase; stubs 16/19 devuelven [] y siguen disabled en seed. `recloser_open`: apertura en ventana de mantenimiento = programada (ok); active=null → not_computable. `power_factor_low`: usa abs(pf) (pf puede venir con signo según dirección del flujo).
- T17: persistencia de `inverter_unavailable` verificada con corrientes DC (cadencia 5 min), no con power live instantáneo. "Todos caídos" → ok excluded (lo cubre project_no_generation); "todas las strings en 0" → ok (nivel inversor, no string). Reglas 3 y 7 dependen de KEYWORDS sobre `state` (DERATING_KEYWORDS/ISOLATION_KEYWORDS en inverter.py/strings.py) — solo se ha visto "Grid-connected"; ampliar keywords cuando aparezcan estados de falla reales o el backend confirme el vocabulario. `rules/helpers.py`: poa_sustained_above (tri-valor True/False/None), window_average, dev_name→external_id.
- T16: "3 intervalos consecutivos" interpretado como frozen_intervals × 15 min (intervalo IEC), NO 3 puntos de cadencia cruda. Congelado = max-min < 1e-6 con ≥3 puntos. data_frozen NO evalúa POA (evita duplicar poa_invalid); potencia 0 constante tampoco cuenta (la cubre project_no_generation). Reglas 11/12 ESCRIBEN NonComputableInterval con get_or_create + floor (60min pr, 15min availability) — idempotente entre ticks. Regla 11 no aplica sin medidor quoia (sin frontera no hay PR contractual). PoaInvalid con POA=0 + generación usa umbral fijo 5 kW.
- T15: `ctx.inverter_model(external_id)` mapea al modelo plants.Inverter (cache). `meter_comm_lost` exige inversores VIVOS para disparar (si todo caído → not_computable, no se puede aislar el medidor); reusa umbral de `inverter_comm_lost` vía ctx.params. Mientras quoia siga roto (500), la regla 8 vive en not_computable — comportamiento correcto sin Bloqueada.
- T14: circuito completo engine→Discord listo: `apps/alarms/tasks.py` (dispatch_evaluations fan-out + evaluate_project con lock `cache.add` TTL 270s y notifier=dispatcher.notify). `WebhookNotConfigured` NO reintenta (error de config); `RequestException` sí (backoff, máx 5). GET al webhook de Discord devuelve `channel_id` → cacheado en canal + snapshot por log. Settings: CACHES Redis agregado (locks entre workers; tests usan LocMemCache). FALTA: beat schedule por defecto (crear PeriodicTask al desplegar o en T21) y crear el NotificationChannel inicial (admin o data migration T21).
- T13: convenciones de exclusión fijadas: mantenimiento → `ok reason="excluded:maintenance"`; meteo inválida (flags fase 1/2) → `not_computable`; recloser abierto → `ok excluded:recloser_open` (regla 17 la cubre); POA < umbral → `ok excluded:low_irradiance`. **relay.active=null o sin relay NO bloquean** (estado a evidence). Tensiones del relay NO se usan (u_a/b/c=0 con planta operando en fixture real — semántica sin validar con backend). `MIN_WINDOW_POINTS=3` para "sostenido". Umbral potencia≈0: capacity×power_zero_ratio, fallback 1 kW sin capacity.
- T12: patrón de regla validado e2e (engine real + regla real + client mockeado). Convenciones: proyecto SIN el equipo → `return []` (la regla no aplica, cero ruido); API caída → `not_computable`. Nuevos módulos de reglas DEBEN importarse en `apps/alarms/rules/__init__.py` para que @register corra. test_engine.py aísla el registro con fixture autouse `isolated_rules` (las reglas reales no corren ahí con MagicMock) — replicar el patrón si se crean más tests de engine puro.
- T11: convención de tiempo: `ctx.now` es NAIVE hora local del proyecto (la API entrega naive local) — comparar series directo; para DB (aware) el engine usa `django.utils.timezone.now()`. `RuleOutcome.inverter_external_id` permite al engine resolver el FK. Severidad solo ESCALA, nunca baja. `evaluate_project(notifier=...)` — el dispatcher se conecta en T14. `validate_registry()` lista códigos sin clase (`alarm_sla_breach` excluido: corre en check_sla). Reglas de test: registrar con @register y `RULES.pop` en teardown. `ctx.poa_series()` prefiere weather.irradiation_poa, fallback power.irradiance.
- T10: `channel.webhook_url` lee `os.environ[env_key]` en runtime (django-environ carga el .env a os.environ al importar settings). Unicidad (alarm, channel, event) EXCLUYE `sla_reminder` (puede repetirse). `channel.accepts(severity)` con `Severity.rank`. min_severity default = medium.
- T09: catálogo en `apps/alarms/catalog.py` (fuente única, importado por la migración 0002). El seed corre en TODOS los tests con DB → tests de modelo NO deben crear reglas con códigos reales (usar `test_rule`). `pr_inputs_missing` quedó en grupo hourly (valida ventanas horarias). Severidades duales del Excel: 13/16/18/19 → medium, 15/20 → high.
- T08: `nulls_distinct=False` NO funciona en sqlite → NonComputableInterval usa DOS constraints parciales (inverter null / not null). `Severity.rank()` para escalamiento. `Alarm.build_dedup_key(code, ext_id, *suffixes)` ignora sufijos vacíos. Constraint de dedup: `~Q(status="resolved")` — ACTIVE y ACKNOWLEDGED cuentan como abiertas. Admin ya tiene actions ack/resolve manual.
- T07: smoke e2e real OK: 77 proyectos, 314 inversores, 0 errores. La API devuelve **`installed_capacity="Desconocida"` (string) y campos numéricos vacíos** en algunos proyectos → `schemas.as_float()` los coerce a None; usar as_float para TODO campo numérico que venga de la API. `sync_catalog` NO toca `monitoring_enabled` (override local). ⚠️ Docker sin permisos para el usuario (`permission denied ... docker.sock`) → smokes con sqlite (`DATABASE_URL=sqlite:////...`); pedir al usuario `sudo usermod -aG docker $USER` antes del e2e final con postgres.
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
