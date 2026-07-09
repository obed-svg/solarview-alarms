# Plan: Módulo de alarmas SolarView (Fase 1)

## Contexto

Repo greenfield (`solarview-alarms`): sistema que lee datos de la API de monitoreo SolarView, evalúa periódicamente las condiciones de alarma del Excel de requerimientos (solo **Fase = 1**), persiste alarmas en PostgreSQL y notifica a Discord. Luego se agregarán APIs DRF para el frontend (fuera de alcance, pero el diseño lo prevé).

**Decisiones tomadas:**
- Django + PostgreSQL; Celery + Celery Beat (Redis) para evaluación periódica
- API externa SIEMPRE por el alias **`/monitoring/`** (no `/api/`)
- `.env` (bloqueado para Claude leerlo directo; el código lo lee en runtime con django-environ): `static_token` = token API, `webhook_discord` = webhook Discord
- No replicar series de tiempo: cada alarma guarda snapshot JSON de "evidencia"
- Notificaciones: **solo Discord ahora, Gmail eventualmente** → modelo simple con campos explícitos, sin config JSON genérico
- Cada notificación guarda el **channel ID** de Discord al que se envió (trazabilidad si cambia el canal)
- **Correcciones de API**: `/inverter/{id}/` NO recibe `variable` (error de la doc). Voltajes DC → `/project/{id}/measurements-dc/`; AC → `/project/{id}/measurement/`
- **Frecuencias reales**: inversores cada 5 min; weather y medidor (quoia) varían de 1 min a 1 h → umbrales de "sin comunicación" configurables por proyecto, se calibran tras sondear las APIs
- **T_mod era incierto en el diseño inicial** → resuelto después (T22): T_mod = `temperature_POA` (sensor del panel, confirmado por el usuario). Regla 16 activa desde la migración 0004; la regla 11 exige T_mod cuando el proyecto tiene estación
- **Sondear las APIs reales ANTES de implementar** cada consumo (token ya en `.env`)
- Implementación ejecutada con **`/loop`** (ver sección final)

## Estructura del proyecto

```
config/                       # settings (base/dev/prod), celery.py, urls.py
integrations/solarview/       # cliente HTTP puro de la API externa (NO es app Django: no tiene modelos)
apps/plants/                  # espejo/cache local de proyectos e inversores de la API
apps/alarms/                  # catálogo de reglas, alarmas, motor de evaluación
apps/notifications/           # canales Discord/Gmail + log de envíos
docker-compose.yml            # postgres:16, redis:7
```

## ¿Qué es `apps/plants` y por qué existe? (pregunta 1 y 3)

**Sí: es un cache local de lo que trae la API** (`/monitoring/project/` y `/monitoring/project/{id}/inverter/`). Un task Celery (`sync_catalog`, cada hora) trae la lista y hace upsert por `external_id`.

Por qué no consultar la API cada vez:
1. **Las alarmas necesitan ForeignKeys estables.** Una fila `Alarm` debe apuntar a un proyecto/inversor de forma permanente. Si solo guardáramos el ID externo como número suelto, no habría integridad referencial ni joins eficientes para el futuro frontend ("dame todas las alarmas del proyecto X").
2. **Consultar alarmas históricas no debe depender de que la API externa esté viva.**
3. Permite colgar configuración propia por proyecto (umbrales, ventanas de mantenimiento, `monitoring_enabled`).

**No se crea ningún modelo nuevo para NotificationChannel** (pregunta 3): la relación canal↔proyecto reutiliza `plants.Project`. Y para Fase 1 ni siquiera eso: un solo canal Discord global (ver sección notificaciones).

Los **strings** (pv1, pv2…) NO se modelan como tabla: la API los expone como claves dentro del inversor; en la alarma se identifican con texto (`component_id = "pv3"`).

## Modelos, campo por campo (pregunta 2)

### `plants.Project` — espejo de un proyecto de la API
| Campo | Tipo | Para qué |
|---|---|---|
| `external_id` | int, unique | El `id` que usa la API. Clave de sincronización. |
| `name`, `plant_code`, `weather_plant_code` | char | Copiados de la API (identificación y consultas a weather). |
| `installed_capacity_kw` | decimal | Para umbrales relativos (ej. "potencia ≈ 0" = < 0.5% de capacidad). |
| `latitude`, `longitude` | decimal | Calcular horario solar (librería astral) para reglas que solo aplican de día. |
| `timezone` | char, default America/Bogota | Interpretar timestamps de la API. |
| `monitoring_enabled` | bool | Apagar la evaluación de un proyecto sin borrarlo. |
| `raw` | JSON | Payload completo de la API tal cual llegó (debug/futuro). |
| `synced_at` | datetime | Última sincronización. |

### `plants.Inverter` — espejo de un inversor
| Campo | Tipo | Para qué |
|---|---|---|
| `project` | FK Project | A qué planta pertenece. |
| `external_id` | int | El `id` de la API. Único junto con `project`. |
| `dev_name` | char | Nombre visible ("Inversor 1"). |
| `is_active` | bool | Si desaparece de la API, se marca inactivo (no se borra: las alarmas históricas lo referencian). |
| `raw`, `synced_at` | JSON, datetime | Igual que en Project. |

### `plants.MaintenanceWindow` — ventanas de mantenimiento
Registra "este proyecto/inversor está en mantenimiento de X a Y". Es la fuente para la exclusión "no activar si hay mantenimiento registrado" que piden varias alarmas. Se gestiona por Django admin. Campos: `project`, `inverter` (opcional: null = todo el proyecto), `starts_at`, `ends_at`, `reason`, `created_by`.

### `alarms.AlarmRule` — catálogo: UNA fila por tipo de alarma (20 filas, seed por data migration)
| Campo | Tipo | Para qué |
|---|---|---|
| `code` | slug, unique | Identificador estable (`inverter_comm_lost`). Une la fila de DB con la clase Python que implementa la lógica. |
| `name`, `description` | char/text | Texto del Excel, para admin y notificaciones. |
| `category` | choices | Proyecto / Inversor / DC-strings / Medidores / Datos / Meteo / Red / Calidad / O&M. |
| `component_type` | choices | Sobre qué se dispara: PROJECT, INVERTER, STRING, METER, WEATHER_STATION, RELAY. |
| `default_severity` | choices | Crítica/Alta/Media, del Excel. |
| `enabled` | bool | Interruptor global. La regla 19 (THD) vive en `False` (la API no expone THD); la 16 (T_mod) se seedeó en `False` y se habilitó por migración (0004) al definirse T_mod = `temperature_POA`. |
| `default_params` | JSON | Umbrales por defecto (criterios COX): `{"poa_min_wm2": 100, "persistence_minutes": 15, ...}`. En JSON porque cada regla tiene parámetros distintos. |
| `rule_group` | fast/hourly | Con qué periodicidad se evalúa: cada 5 min o cada hora. |
| `auto_resolve` | bool | Si la alarma se cierra sola cuando la condición desaparece. |

### `alarms.RuleConfig` — override de umbrales POR proyecto (explicado)

Problema que resuelve: los umbrales COX son globales (POA > 100, staleness meteo 5 min…), pero cada planta es distinta — una reporta weather cada 2 min, otra cada 30. Si el umbral vive solo en el catálogo, ajustar una planta obligaría a cambiar el umbral de TODAS.

Cómo funciona: es una tabla de **excepciones**. Por defecto está **vacía** y todos los proyectos usan los `default_params` de `AlarmRule`. Cuando un proyecto necesita un valor distinto, se crea UNA fila (por admin) solo con lo que cambia:

```
AlarmRule(code="weather_comm_lost").default_params = {"stale_minutes": 5}   # global

RuleConfig(rule=weather_comm_lost, project=La Guajira).params = {"stale_minutes": 45}
# La Guajira reporta weather cada 30 min → sin esta fila tendría alarma falsa permanente
```

Al evaluar, el motor resuelve: `{**rule.default_params, **config.params}` — los params del proyecto pisan solo las claves que definen; el resto sigue heredando del catálogo. El campo `enabled` nullable funciona igual: `null` = hereda del catálogo, `True/False` = fuerza encender/apagar esa regla solo para ese proyecto. Campos: `rule` FK, `project` FK, `enabled` (null=hereda), `params` JSON. Unique (rule, project).

### `alarms.Alarm` — una alarma concreta que ocurrió
| Campo | Tipo | Para qué |
|---|---|---|
| `rule` | FK AlarmRule | Qué tipo de alarma es. |
| `project`, `inverter` | FK (inverter nullable) | Dónde ocurrió. |
| `component_type`, `component_id` | char | Precisión fina: `("STRING", "pv3")`. Vacío si es a nivel proyecto. |
| `severity` | choices | Copiada de la regla al crear; puede escalar (dif. medidores: 3%→alerta, 5%→alta). |
| `status` | ACTIVE / ACKNOWLEDGED / RESOLVED | Ciclo de vida: activa → alguien la vio (ack) → resuelta. |
| `dedup_key` | char | `"{rule.code}:{project.external_id}:{inverter?}:{component_id?}"`. **Con constraint de unicidad parcial** (solo cuenta si status ≠ RESOLVED): garantiza a nivel de DB que nunca haya 2 alarmas abiertas del mismo tipo sobre el mismo componente. Es el mecanismo anti-duplicados: si la condición persiste 2 horas, hay UNA alarma, no 24. |
| `triggered_at` | datetime | Cuándo se disparó. |
| `last_seen_at` | datetime | Último tick en que la condición seguía activa. Se actualiza cada evaluación. |
| `acknowledged_at/by`, `resolved_at` | datetime/char | Auditoría del ciclo de vida. |
| `resolution_type` | AUTO / MANUAL | Se resolvió sola o la cerró una persona. |
| `evidence` | JSON | Snapshot de los datos que justificaron el disparo (ej. `{"poa": 640, "total_power_kw": 0.1, "window": [...]}`). Inmutable. |
| `last_evidence` | JSON | Igual pero del último tick (para ver el estado más reciente). |
| `occurrence_count` | int | Cuántos ticks consecutivos se ha visto la condición. |

### `alarms.NonComputableInterval` — salida de las reglas 11/12
Cuando faltan datos para PR o disponibilidad, el requerimiento dice "marcar el período como no calculable" (no inventar 0% ni 100%). Esta tabla es esa marca: `project`, `inverter` (nullable), `metric` (pr/availability), `interval_start/end`, `missing_inputs` (ej. `["poa"]`). El futuro frontend la consulta para pintar huecos grises.

### `alarms.EvaluationRun` — bitácora de cada ejecución
Una fila por (proyecto, tick): `started_at`, `finished_at`, `status` (SUCCESS/PARTIAL/FAILED), `stats` JSON (qué reglas dispararon, cuáles fallaron, cuántas alarmas se abrieron/resolvieron). Es el "¿está corriendo bien el sistema?" en el admin, y evita depender de logs de Celery.

### `notifications` — simplificado a Discord + Gmail futuro (pregunta 2 y 3)
Sin config JSON genérico ni M2M de filtros. Estructura mínima:

```python
class NotificationChannel(models.Model):
    KIND = [("discord", "Discord"), ("gmail", "Gmail")]
    name = CharField(unique=True)              # "ops-discord"
    kind = CharField(choices=KIND)
    env_key = CharField(default="webhook_discord")  # NOMBRE de la var en .env con el secreto
                                               # (el secreto nunca va en DB). Permite en el futuro
                                               # varios canales por proyecto, cada uno apuntando a
                                               # su propia variable (webhook_discord_guajira, etc.)
    discord_channel_id = CharField(blank=True) # cacheado: GET al webhook URL devuelve channel_id
    recipients = TextField(blank=True)         # solo para gmail futuro: correos separados por coma
    min_severity = CharField(default="MEDIUM") # no notificar por debajo de esto
    enabled = BooleanField(default=True)

class NotificationLog(models.Model):
    alarm = FK(Alarm)
    channel = FK(NotificationChannel)
    event = CharField()                        # opened | escalated | resolved
    target_channel_id = CharField()            # SNAPSHOT del channel de Discord al momento del envío
                                               # (trazabilidad pedida: si mañana cambian el webhook a otro
                                               # canal, el historial conserva a dónde se envió cada una)
    status = CharField(default="pending")      # pending | sent | failed
    attempts = IntegerField(default=0)
    payload = JSONField()                      # el embed exacto que se envió
    last_error = TextField(blank=True)
    created_at, sent_at = DateTimeFields()
    # unique (alarm, channel, event) → nunca se notifica 2 veces el mismo evento de la misma alarma
```

¿Por qué existe el modelo si el webhook vive en `.env`? Solo por lo que SÍ es estado operativo y no secreto: apagar/encender el canal y ajustar `min_severity` desde el admin sin redesplegar, cachear el `discord_channel_id`, y darle un FK a `NotificationLog`. El secreto (URL del webhook) se lee siempre del `.env` en el momento del envío.

Si algún día se necesita canal-por-proyecto, se agrega un M2M a `plants.Project` entonces — no ahora.

## Motor de evaluación — explicado (preguntas 4, 5, 6, 7)

### "Contrato de regla" (pregunta 5)

"Contrato" = la interfaz que TODA regla debe cumplir para que el motor las trate uniformemente. Cada una de las 19 reglas del engine es una clase Python en `apps/alarms/rules/` (la 20, SLA, corre aparte en `check_sla`), y todas se ven así:

```python
@register                                   # se anota en un dict global {code: clase}
class InverterCommLost(BaseRule):
    code = "inverter_comm_lost"             # une esta clase con la fila AlarmRule de la DB

    def evaluate(self, ctx) -> list[RuleOutcome]:
        params = ctx.params(self.code)      # umbrales ya mezclados (default + override del proyecto)
        outcomes = []
        for inv in ctx.inverters_live():    # datos ya traídos de la API (ver EvaluationContext)
            age = ctx.now - inv.last_data_time
            if age > timedelta(minutes=params["inverter_stale_minutes"]):
                outcomes.append(RuleOutcome(
                    dedup_suffix=f"inv:{inv.external_id}",
                    status="firing",
                    evidence={"last_data": str(inv.last_data_time), "age_min": age.seconds // 60},
                ))
            else:
                outcomes.append(RuleOutcome(dedup_suffix=f"inv:{inv.external_id}", status="ok"))
        return outcomes
```

El motor no sabe nada de inversores ni POA: solo recorre las clases registradas, llama `evaluate(ctx)` y procesa los `RuleOutcome`. Agregar la regla 15 = escribir una clase nueva; el motor no cambia. `RuleOutcome` tiene: `dedup_suffix` (qué componente), `status` (ok/firing/not_computable), `severity` opcional (para escalar), `evidence` (JSON justificante), `reason` (por qué no se pudo calcular o qué exclusión aplicó).

### EvaluationContext (pregunta 6)

Problema que resuelve: en un tick, las ~17 reglas de un proyecto necesitan datos que se solapan muchísimo (la POA la usan las reglas 1, 2, 5, 6, 9, 13, 15…). Si cada regla llamara a la API por su cuenta serían ~40 requests por proyecto cada 5 minutos.

`EvaluationContext` es un objeto que se crea UNA vez por (proyecto, tick) y centraliza el acceso a datos con **cache perezoso**: la primera regla que pide `ctx.poa_window(20)` dispara el request real; las siguientes reciben la copia en memoria. Resultado: ~6-8 requests por proyecto por tick, y las reglas quedan limpias (piden datos semánticos, no URLs).

Además expone helpers compartidos: `ctx.params(code)` (umbrales resueltos), `ctx.is_solar_hours(margin_minutes=N)` (astral con lat/lon; el margen recorta amanecer/ocaso para evitar flapping en los bordes del día, y ante coordenadas inválidas — hay proyectos con lat/lon invertidas — cae al horario fijo), `ctx.in_maintenance(inverter)` (consulta MaintenanceWindow), `ctx.flag_active("inverter_comm_lost", "inv:5")` (¿otra regla ya disparó en este tick? — para exclusiones). Si un request a la API falla, el método devuelve un sentinel `Unavailable(reason)` en vez de explotar: cada regla que dependía de ese dato reporta `not_computable` y las demás siguen.

### Semántica tri-estado (pregunta 7)

Cada regla, para cada componente, responde una de TRES cosas — y el motor reacciona distinto:

| La regla dice | Significa | El motor hace |
|---|---|---|
| `firing` | Verifiqué la condición y ES anormal | Si no hay alarma abierta con ese `dedup_key` → crear + notificar "opened". Si ya hay → actualizar `last_seen_at`, `last_evidence`, `occurrence_count++` (NO crear otra). Si la severidad subió → escalar + notificar. |
| `ok` | Verifiqué y está normal | Si hay alarma abierta → **resolver de inmediato**: RESOLVED/AUTO + notificar "resolved" en ese mismo tick. El usuario ve la corrección al instante. |
| `not_computable` | NO PUDE verificar (faltan datos, API caída, meteo inválida) | **No tocar nada**: no crea alarma (no sabemos si hay falla) y no resuelve la existente (que no haya datos no significa que se arregló). Registra `NonComputableInterval` si aplica. |

El tercer estado es la diferencia clave con un sistema binario: sin él, una caída de la API resolvería falsamente todas las alarmas abiertas, o peor, dispararía falsas alarmas de "sin generación".

Nota sobre resolución inmediata (decisión del usuario): si una condición parpadea (falla → se recupera un tick → falla de nuevo), se generarán filas de alarma separadas, cada una con su ciclo abrir/resolver. Eso es aceptable y hasta útil: el historial muestra fielmente cada episodio. El falso positivo de DISPARO ya lo mitiga la persistencia de la condición (los "15 min sostenidos" de la ventana antes de abrir la alarma).

### Ventanas imprecisas y lag de escritura (pregunta 4)

Realidad: consultamos cada 5 min, pero el backend escribe con retraso (delay de consulta + escritura en su DB), y las fuentes tienen cadencias distintas (inversores 5 min; weather/quoia entre 1 min y 1 h). Medidas concretas:

1. **Ventana desplazada**: nunca evaluar hasta `now`; evaluar `[now - persistence - lag, now - lag]` con `data_lag_minutes` configurable (default 3-5 min, se calibra al sondear las APIs). El dato "de hace 1 minuto" que aún no se escribió no cuenta como ausente.
2. **Staleness relativa a la cadencia de cada fuente**: "sin comunicación" = `now - último_timestamp > N × cadencia_esperada + lag`, con N y cadencia por fuente en params (`inverter_expected_cadence_min: 5`, weather/quoia se calibran por proyecto tras el sondeo). El default COX (5 min meteo) se ajustará con datos reales.
3. **Cobertura mínima de ventana**: para "POA > 100 durante 15 min", exigir que la ventana tenga ≥ X% de los puntos esperados; si no, `not_computable` (no adivinar con 2 puntos).
4. **Sin igualdad exacta de timestamps**: nunca se cruzan series por timestamp exacto entre fuentes; se comparan agregados por ventana (promedios, deltas, últimos valores).

### Evaluación en 3 fases dentro del tick

Para implementar exclusiones tipo "no clasificar como falla del inversor si hay comunicación caída": primero se evalúan reglas de **comunicación** (4, 8, 14), luego **calidad de datos** (11, 12, 13, 15), y por último las **eléctricas** (1, 2, 3, 5, 6, 7, 9, 10, 17, 18) — que consultan con `ctx.flag_active(...)` lo que dispararon las fases previas para excluirse. Ej.: regla 2 (inversor no disponible) no dispara si la 4 (comm lost) ya disparó para ese inversor.

## Mapa alarma → endpoints (corregido, todos vía `/monitoring/`)

| # | code | Dedup por | Endpoints |
|---|---|---|---|
| 1 | `project_no_generation` | proyecto | `/project/{id}/power/?total_power=1` (potencia AC + POA), `/project/{id}/relay/` (cerrado + tensiones), `/project/{id}/weather/` (meteo válida) |
| 2 | `inverter_unavailable` | inversor | `/project/{id}/inverter/` (power≈0, state, time), `/project/{id}/measurements-dc/` (tensión DC `vs`), `/project/{id}/measurement/` (tensión AC), `/project/{id}/power/` (POA); comparables = demás inversores del mismo response |
| 3 | `inverter_derating` | inversor | `/project/{id}/inverter/` (state, temperature>100°C, power vs comparables) |
| 4 | `inverter_comm_lost` | inversor | `/project/{id}/inverter/` (campo `time` vs staleness calibrada) |
| 5 | `string_zero_current` | string | `/project/{id}/measurements-dc/?variable=cs` (I≈0, otros >1A), `/project/{id}/inverter/` (activo) |
| 6 | `string_low_current` | string | `/project/{id}/measurements-dc/?variable=cs` (<80% promedio mismo inversor), POA |
| 7 | `dc_isolation_low` | inversor | `/project/{id}/inverter/` (`state` con código de aislamiento — verificar en sondeo) |
| 8 | `meter_comm_lost` | proyecto | `/project/{id}/quoia_measurements_history/` SIN params (staleness) vs `/project/{id}/inverter/` (inversores sí reportan) |
| 9 | `meter_no_increment` | proyecto | `/quoia_measurements_history/` SIN params (Σ energía por intervalo ≈0/60min), `/generation/` (inversores generando), POA |
| 10 | `meter_inverter_mismatch` | proyecto | `/generation/` vs Σ intervalos de `/quoia_measurements_history/` por hora; >3% alerta, >5% alta |
| 11 | `pr_inputs_missing` | proyecto | Derivada: presencia de energía AC (quoia), POA, P_DC (measurements-dc) y T_mod (`temperature_POA`) cuando el proyecto tiene estación. |
| 12 | `availability_inputs_missing` | inversor | Derivada: POA válida + potencia/estado/timestamp de `/project/{id}/inverter/` |
| 13 | `data_frozen` | proyecto/señal | Ventanas de `/power/` y `/weather/` (3 intervalos idénticos en horario solar; sin temperatura de módulo) |
| 14 | `weather_comm_lost` | proyecto | `/project/{id}/weather/` (staleness calibrada por proyecto) |
| 15 | `poa_invalid` | proyecto | `/weather/` (POA<0/inválida/congelada) cruzada con `/power/` (POA=0 con generación) |
| 16 | `tmod_invalid` | proyecto | `/project/{id}/weather/` (`temperature_POA` nula/congelada/fuera de rango/incoherente vs ambiente con POA alta; solo horario solar; sin estación no aplica) |
| 17 | `recloser_open` | proyecto | `/project/{id}/relay/` (abierto/trip en horario solar), `/relay/historical/`; programado vs disparo vía MaintenanceWindow |
| 18 | `power_factor_low` | proyecto | `/project/{id}/relay/` (pf<0.95, kw para descartar baja carga; unidades normalizadas por `rules/relay_normalize.py`; solo horario solar) |
| 19 | `thd_abnormal` | — | Stub `enabled=False`: la API no expone THD |
| 20 | `alarm_sla_breach` | alarma origen | Sin API externa: tabla `Alarm` local (ACTIVE sin ack > sla_ack_minutes) |

## Tareas Celery (pregunta 11 incluida)

**¿Qué es `evaluate_project`?** Es LA tarea central: recibe un `project_id` y un grupo de reglas, construye el `EvaluationContext` de ese proyecto, corre las 3 fases de reglas, hace los upserts de alarmas y escribe el `EvaluationRun`. Todo lo que se describió del motor ocurre dentro de esta tarea. Se ejecuta en un worker de Celery, lo que da: paralelismo (10 proyectos = 10 tareas simultáneas en vez de un for secuencial), aislamiento (si el proyecto 3 truena, los demás siguen) y timeout individual.

| Task | Schedule | Qué hace |
|---|---|---|
| `dispatch_evaluations("fast")` | */5 min | Solo hace fan-out: por cada Project con `monitoring_enabled` → `evaluate_project.delay(pid, "fast")` |
| `dispatch_evaluations("hourly")` | cada hora | Igual pero grupo hourly (reglas 9, 10 con ventanas de 60 min) |
| `evaluate_project(pid, group)` | — | Ver arriba. Lock Redis `evaluate:{pid}:{group}` no-bloqueante: si el tick anterior sigue corriendo, este se salta (nunca solapar). `soft_time_limit=240s`. Try/except POR regla: una regla rota → `EvaluationRun=PARTIAL`, las demás corren. |
| `plants.sync_catalog` | cada hora | Upsert de proyectos/inversores desde la API |
| `send_notification(log_id)` | on-demand | POST a Discord; autoretry con backoff, máx 5 intentos; actualiza NotificationLog |
| `check_sla` | */10 min | Regla 20: alarmas ACTIVE sin ack > umbral → abre `alarm_sla_breach` |

Errores de API en `evaluate_project`: sin retry agresivo — el siguiente tick de 5 min es el retry natural.

## Cliente SolarView (`integrations/solarview/`)

- `requests.Session` + `Retry(429/502/503/504)`, timeout (5, 30). Base: `https://{SOLARSOLARVIEW_BASE_URL}/monitoring/...` (llave real del `.env`, sin esquema; settings acepta `SOLARVIEW_BASE_URL` como fallback). Auth con `static_token` desde `.env`.
- Un método público por endpoint que valida el envelope (`success`, `error`) y devuelve dataclasses tipadas — las reglas nunca ven JSON crudo.
- Excepciones: `SolarViewAPIError`, `SolarViewTimeout`, `SolarViewAuthError` (esta no reintenta).
- **Sondeo primero (pregunta 10)**: antes de escribir el cliente definitivo, scripts de sondeo contra la API real (token cargado en runtime desde `.env`, nunca impreso ni pegado en el chat) que graban los responses reales como fixtures JSON. Con eso: se confirma formato de timestamps, cadencia real de weather/quoia por proyecto, códigos de `state` del inversor (¿distingue derating/aislamiento?), y estructura exacta de measurements-dc. Los fixtures alimentan los tests.

## Notificaciones Discord

Embed con color por severidad, título = nombre de la regla, campos = proyecto/componente/evidencia resumida/timestamp. Al crear o refrescar el canal: GET al webhook URL (Discord devuelve `channel_id`) → se cachea en `NotificationChannel.discord_channel_id` y cada envío lo copia a `NotificationLog.target_channel_id`. Eventos notificados: opened, escalated, resolved (filtrados por `min_severity`).

## Preparado para el frontend (no implementar)

DRF instalado, router reservado `/api/v1/`. `Alarm` ya tiene todo lo que un frontend necesita (filtros por índices, timeline, evidence, occurrence_count). Mientras tanto el **Django admin es la UI operativa**: filtros por status/severity/project, actions de ack/resolve, edición de umbrales, canales y schedules.

## Ajustes post-diseño (aprendidos con datos reales, 2026-07-08)

El diseño anterior se validó contra la API y las plantas reales; estos ajustes
anti-ruido no estaban en el plan original (detalle por tarea en `ROADMAP.md`,
sección Post-COMPLETADO):

- **T_mod definido** (T22): `temperature_POA` = temperatura del panel. Regla 16
  `tmod_invalid` implementada y habilitada (migración 0004); la regla 11 exige
  T_mod cuando el proyecto tiene estación. 19/20 reglas activas (solo THD off).
- **Gate de horario solar con margen** (T23): los inversores se "duermen" al
  ocaso y generaron una ola de ~193 falsas alarmas (`inverter_comm_lost`,
  `pr_inputs_missing`). Las reglas 4 y 12 solo evalúan dentro de
  `[amanecer+45, ocaso-45]`; la 11 exige ventana horaria completamente diurna
  (margen 30). Params en migración 0005.
- **Auto-test de aislamiento no es falla** (T24): el state
  `"Standby: insulation resistance detecting"` es rutinario → la regla 7 exige
  calificador (low/fault/abnormal/fail). La regla 6 exige baseline comparable
  ≥ `comparable_min_current_a` (goteo de 0.1 A al atardecer no es string
  degradado). De 129 alarmas iniciales quedaron 2 legítimas.
- **Fallback de horario solar** (T25): la API entrega lat/lon invertidas en al
  menos un proyecto (astral explota con "Sun never reaches 6 degrees") →
  `is_solar_hours` cae al horario fijo ante `ValueError`.
- **Gate nocturno en `power_factor_low`** (T26): el pf fuera de horario solar
  no es señal accionable.
- **Normalización heurística del relay** (T27): la API no informa marca/modelo
  del medidor y `kw` llega en escalas inconsistentes (se sospecha W vs kW).
  `rules/relay_normalize.py` resuelve la unidad por plausibilidad física
  contra la capacidad instalada (desempate por potencia de inversores); pf
  ÷100 si viene en %; ambigüedad irresoluble → `not_computable` (nunca
  adivinar). `evidence` conserva `kw_raw` + notas de normalización para
  auditoría.
- **Quoia descifrado** (T28): el `_history` funciona **sin parámetros de
  fecha** (cualquier query param dispara un 500 del backend) y devuelve las
  últimas ~24 h. El payload es energía POR INTERVALO (~15 min), no contador
  acumulado → la energía de frontera en ventana es la SUMA de intervalos
  (con guardia de cobertura), no `último − primero`. 26/77 proyectos
  entregan datos reales; el endpoint live `/quoia_measurements/` existe pero
  está roto server-side (500 `"-1"` en todos).

## Arquitectura de ejecución con `/loop`

### Archivo de estado: `ROADMAP.md` en la raíz del repo

Cola de trabajo y memoria persistente entre iteraciones (el repo es la memoria: sobrevive a compactaciones de contexto). El bloque de abajo es el snapshot con el que arrancó el loop; el estado real y las notas acumuladas viven en `ROADMAP.md` (hoy: COMPLETADO):

```markdown
# ROADMAP — solarview-alarms
## Estado: EN PROGRESO
- [ ] T01 Scaffolding: Django + settings + docker-compose + pyproject + pytest/ruff
- [ ] T02 config/celery.py + colas + healthcheck
- [ ] T03 SONDEO APIs reales: scripts que consultan /monitoring/ con static_token, graban
      fixtures JSON reales, documentan cadencias weather/quoia y códigos de state. GATE:
      lo aprendido ajusta params por defecto y puede mover tareas a Bloqueadas.
- [ ] T04 Cliente SolarView: base (/monitoring/, envelope, excepciones) + tests con fixtures reales
- [ ] T05 Cliente SolarView: métodos por endpoint + schemas
- [ ] T06 App plants: modelos + migraciones + admin
- [ ] T07 plants.sync_catalog (primer task Celery e2e)
- [ ] T08 Modelos alarms + migraciones + admin
- [ ] T09 Seed 20 AlarmRule con params COX (16 y 19 disabled)
- [ ] T10 Modelos notifications + admin
- [ ] T11 Engine: base + registry + EvaluationContext (cache, Unavailable, lag windows)
- [ ] T12 Regla piloto 14 weather_comm_lost + upsert/dedup/auto-resolve + tests
- [ ] T13 Regla piloto 1 project_no_generation (ventanas/exclusiones/fases) + tests
- [ ] T14 Canal Discord + dispatcher + NotificationLog + target_channel_id + tests
- [ ] T15 Reglas comunicación: 4, 8
- [ ] T16 Reglas calidad datos: 11, 12, 13, 15
- [ ] T17 Reglas inversor/strings: 2, 3, 5, 6, 7
- [ ] T18 Reglas medidores: 9, 10
- [ ] T19 Reglas red/calidad: 17, 18 (+ stubs 16, 19)
- [ ] T20 check_sla (regla 20) + escalamiento
- [ ] T21 Hardening: locks, EvaluationRun dashboard, tuning
## Bloqueadas
(si una tarea falla 2 intentos o requiere decisión humana, va aquí con diagnóstico)
## Notas entre iteraciones
(gotchas que la siguiente iteración debe conocer: cadencias reales, formatos, etc.)
```

### Prompt del loop

```
/loop Lee ROADMAP.md. Toma la PRIMERA tarea sin marcar (salta Bloqueadas).
Impleméntala con TDD. Ejecuta pytest + ruff:
- Verde: commit atómico ("T0X: <desc>"), marca [x], anota gotchas en Notas.
- Falla tras 2 intentos: mueve a Bloqueadas con diagnóstico, sigue con la siguiente.
Sin tareas pendientes: Estado = COMPLETADO y termina el loop.
Una sola tarea por iteración.
```

### Reglas del patrón
- 1 tarea = 1 iteración = 1 commit atómico (git log refleja el ROADMAP)
- Gate de calidad: nada se marca sin pytest verde
- T03 (sondeo) va temprano deliberadamente: valida doc vs realidad antes de construir encima
- Bloqueadas en vez de retry infinito: lo que necesita decisión humana se aparca con diagnóstico
- El usuario puede interrumpir cuando sea; ROADMAP.md + git log muestran el avance exacto

## Verificación

- `docker compose up -d`, `migrate`, `pytest` verde en cada tarea
- T03: sondeo real confirma auth, envelope, cadencias (sin exponer el token en el chat)
- Por regla: tests unitarios con fixtures reales (caso firing / ok / not_computable / exclusión)
- E2E: `celery -A config worker -B` contra la API real → EvaluationRun en admin, alarma con evidencia, dedup al siguiente tick (occurrence_count++ sin fila nueva), auto-resolve inmediato al desaparecer la condición, mensaje en Discord con `target_channel_id` en NotificationLog
