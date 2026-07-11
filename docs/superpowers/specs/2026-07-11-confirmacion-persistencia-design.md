# Confirmación por persistencia: estado `pending` antes de notificar

**Fecha:** 2026-07-11
**Motivación:** la vigilancia nocturna del 10-11 jul documentó tres patrones de ruido
con la misma causa raíz — la alarma abre y notifica en el PRIMER ciclo que dispara:

1. Falsos positivos de borde crepuscular/amanecer (SYNLAB 22:00, Acanto 22:30,
   San Pedro 11:55): abren en el último/primer ciclo computable del día, quedan
   zombie toda la noche y escalan SLA de madrugada.
2. Flapping de ciclos de 5 min (`inverter_derating` CC Atenas ×15 en 6 h;
   239 `inverter_comm_lost` + 284 `availability_inputs_missing` resueltas en
   ≤1 ciclo en 6 h vespertinas) — cada apertura re-notifica a Discord.
3. Ciclo diario de medidores: cada reapertura matinal notifica como si fuera nueva.

## Decisiones (con el usuario, 2026-07-11)

| Decisión | Elección | Alternativas descartadas |
|---|---|---|
| Semántica | Estado `pending` en BD desde el primer ciclo; visible en admin; no notifica ni cuenta SLA | Gate solo de notificación (dashboard seguiría mostrando flapping); tabla de rachas aparte (pierde trazabilidad, infraestructura nueva) |
| Umbral | `confirmation_cycles` = 3 (≈15 min) como default en código; `recloser_open` = 1 explícito | N=2 (deja pasar flapping de 2-3 ciclos observado); N=3 estricto (retrasaría la alerta de red más crítica) |
| SLA | Corre desde `confirmed_at` (momento de la notificación); `pending` no cuenta | Desde `triggered_at` (el reloj correría antes de que exista algo que atender) |

## 1. Modelo (`apps/alarms/models.py`)

- `Alarm.Status` gana `PENDING = "pending"`. Toda alarma nueva nace `pending`
  (salvo `confirmation_cycles <= 1`, que nace `active`).
- Campo nuevo `confirmed_at = models.DateTimeField(null=True, blank=True)`:
  momento del paso `pending → active` (= momento de la notificación `opened`).
  `triggered_at` conserva su significado: primer ciclo que disparó (verdad física).
- La constraint `uniq_open_alarm_per_dedup_key` (excluye `resolved`) ya cubre
  `pending` — no se toca. No puede coexistir una pending y una active con la
  misma dedup_key.
- Migraciones: (a) schema — agregar `confirmed_at`; (b) backfill —
  `confirmed_at = triggered_at` para filas existentes no-resueltas (fueron
  notificadas al abrir, así que su confirmación real fue su apertura).

## 2. Parámetro (`catalog.py` + data migration)

- El engine lee `params.get("confirmation_cycles", 3)`. El default vive EN CÓDIGO,
  no en los params seeded — las 19 reglas existentes no necesitan migración de params.
- `recloser_open` recibe `"confirmation_cycles": 1` explícito en sus
  `default_params` del catálogo + data migration para la fila ya seeded.
- Overrides por proyecto: gratis vía RuleConfig/`params_for` existente.

## 3. Engine (`_process_outcome` en `engine.py` — único cambio de flujo)

| Outcome | Estado previo | Acción |
|---|---|---|
| `firing` | sin alarma | Crear `pending`, `occurrence_count=1`, sin notificar. Si `confirmation_cycles <= 1`: crear `active` + `confirmed_at=now` + notificar `opened` |
| `firing` | `pending` | Incrementar `occurrence_count`, refrescar `last_seen_at`/`last_evidence`; si `occurrence_count >= confirmation_cycles`: `status=active`, `confirmed_at=now`, notificar `opened` (una sola vez, con la severidad vigente) |
| `firing` | `active`/`acknowledged` | Igual que hoy (refresh + posible `escalated`) |
| `ok` | `pending` | Resolver silenciosa (`resolution_type=auto`, SIN notificación) |
| `ok` | `active`/`acknowledged` | Igual que hoy (resuelve + notifica `resolved`) |
| `not_computable` | cualquiera | Igual que hoy: no toca nada. Una pending congelada de noche no notifica ni cuenta SLA; al amanecer confirma o muere |

- Escalada de severidad durante `pending`: se actualiza el campo, sin notificación
  `escalated` propia — la notificación de apertura ya sale con la severidad final.
- Semántica de "N ciclos consecutivos": N outcomes `firing` sin un `ok` intermedio.
  El `ok` ES el reset (resuelve la pending). `not_computable` congela: ni suma ni resetea.

## 4. SLA (`check_sla` en `tasks.py`)

- El filtro existente `status=ACTIVE` ya excluye `pending` sin cambios.
- Edad: `now - (alarm.confirmed_at or alarm.triggered_at)`. El fallback cubre
  filas anteriores al backfill o casos borde.

## 5. Observabilidad

- `stats` del ciclo: separar `opened` en `pending_opened`, `confirmed`,
  `pending_discarded` (+ los existentes `updated`, `resolved`).
- Admin: `confirmed_at` visible en detalle; `pending` ya aparece en el filtro
  de status existente.

## 6. Tests (`apps/alarms/tests/`)

1. Firing nuevo → crea `pending`, no notifica.
2. Pending con 3 firings → confirma, notifica `opened` exactamente una vez, `confirmed_at` seteado.
3. Pending + `ok` → resuelta silenciosa, sin notificación.
4. `confirmation_cycles=1` → nace `active` + notifica de inmediato.
5. Escalada en `pending` → sin notificación `escalated`; la apertura lleva la severidad final.
6. `check_sla` ignora `pending` y calcula edad desde `confirmed_at`.
7. `not_computable` sobre `pending` → no incrementa ni resuelve.
8. Backfill: alarma activa preexistente queda con `confirmed_at = triggered_at`.

## Fuera de alcance (deliberado)

- Lado de la resolución intacto — sin histéresis (preferencia explícita del usuario).
- Bug de alarmas huérfanas (HIDRO-MANA cs7 ausente de measurements_dc): fix aparte.
- Ciclo diario de medidores (resolver al anochecer / reabrir al amanecer): decisión
  de producto pendiente; este diseño solo evita que cada reapertura re-notifique
  si dura menos de N ciclos.
- Congelar el reloj SLA fuera de horario atendible: decisión de producto pendiente.
