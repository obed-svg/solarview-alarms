# Confirmación por persistencia (pending → active) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Las alarmas nacen en estado `pending` (sin notificar ni contar SLA) y solo pasan a `active` + notifican tras `confirmation_cycles` (default 3) outcomes `firing` sin un `ok` intermedio; `recloser_open` confirma inmediato.

**Architecture:** Un solo punto de cambio de flujo (`_process_outcome` en `apps/alarms/engine.py`) + campo `confirmed_at` y estado `PENDING` en el modelo `Alarm`. El SLA pasa a contar desde `confirmed_at`. Spec: `docs/superpowers/specs/2026-07-11-confirmacion-persistencia-design.md`.

**Tech Stack:** Django 5 + pytest-django (fixtures existentes en `apps/alarms/tests/`). Los tests corren con `.venv/bin/pytest`. El catálogo se seedea por data migration, así que los tests ven las 20 reglas reales; el engine se testea con la regla sintética `scripted` de `test_engine.py`.

**Contexto no obvio para quien llega de cero:**
- `_process_outcome` recibe `ctx=None` cuando lo llama `check_sla` (tasks.py) — los params deben resolverse con `ctx.params(rule.code) if ctx else rule.params_for(project)`.
- `notifier(alarm, event)` es un callable opcional; los tests lo simulan con `MagicMock()`.
- La constraint `uniq_open_alarm_per_dedup_key` excluye solo `resolved`, así que cubre `pending` sin cambios.

---

### Task 1: Modelo — estado PENDING, campo confirmed_at, migraciones

**Files:**
- Modify: `apps/alarms/models.py:101-104` (choices) y `:126` (campos de Alarm)
- Create: `apps/alarms/migrations/00XX_alarm_confirmed_at.py` (auto)
- Create: `apps/alarms/migrations/00XX_backfill_confirmed_at.py` (a mano)

- [ ] **Step 1: Agregar el estado y el campo al modelo**

En `apps/alarms/models.py`, dentro de `class Alarm`, cambiar:

```python
    class Status(models.TextChoices):
        ACTIVE = "active", "Activa"
        ACKNOWLEDGED = "acknowledged", "Reconocida"
        RESOLVED = "resolved", "Resuelta"
```

por:

```python
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente de confirmación"
        ACTIVE = "active", "Activa"
        ACKNOWLEDGED = "acknowledged", "Reconocida"
        RESOLVED = "resolved", "Resuelta"
```

Y debajo de `last_seen_at = models.DateTimeField()` agregar:

```python
    # Momento del paso pending→active (= momento de la notificación "opened").
    # triggered_at conserva la verdad física: el primer ciclo que disparó.
    confirmed_at = models.DateTimeField(null=True, blank=True)
```

- [ ] **Step 2: Generar la migración de schema**

Run: `.venv/bin/python manage.py makemigrations alarms`
Expected: una migración nueva con `AddField ... confirmed_at` (el cambio de choices no genera operación de DB, solo estado).

- [ ] **Step 3: Crear la migración de backfill**

Run: `.venv/bin/python manage.py makemigrations alarms --empty -n backfill_confirmed_at`

Editar el archivo generado para que quede:

```python
from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    """Las alarmas abiertas existentes fueron notificadas al abrir: su
    confirmación real fue su apertura."""
    Alarm = apps.get_model("alarms", "Alarm")
    Alarm.objects.exclude(status="resolved").filter(
        confirmed_at__isnull=True
    ).update(confirmed_at=F("triggered_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("alarms", "00XX_alarm_confirmed_at"),  # ← ajustar al nombre real del Step 2
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
```

- [ ] **Step 4: Migrar y verificar el backfill**

Run: `.venv/bin/python manage.py migrate alarms`
Luego: `.venv/bin/python manage.py shell -c "from apps.alarms.models import Alarm; print(Alarm.objects.exclude(status='resolved').filter(confirmed_at__isnull=True).count())"`
Expected: `0`

- [ ] **Step 5: Correr la suite para confirmar que nada se rompió**

Run: `.venv/bin/pytest apps/alarms/tests/ -q`
Expected: PASS completo (el campo es opcional y el estado nuevo aún no lo usa nadie).

- [ ] **Step 6: Commit**

```bash
git add apps/alarms/models.py apps/alarms/migrations/
git commit -m "feat: estado pending y confirmed_at en Alarm (spec confirmación por persistencia)"
```

---

### Task 2: Engine — nace pending, confirma en N ciclos, notifica una sola vez

**Files:**
- Modify: `apps/alarms/tests/test_engine.py` (fixture `scripted_rule` + clase nueva)
- Modify: `apps/alarms/engine.py:54` (stats) y `:159-196` (rama firing)
- Modify: `apps/alarms/tasks.py:53` (stats de check_sla)
- Modify: `apps/alarms/tests/test_check_sla.py` (los breaches ahora nacen pending)

- [ ] **Step 1: Blindar los tests legacy del engine**

En `apps/alarms/tests/test_engine.py`, en la fixture `scripted_rule`, cambiar:

```python
    AlarmRule.objects.create(
        code="scripted", name="Scripted", category="project", component_type="project",
        default_severity=Severity.HIGH, default_params={},
    )
```

por:

```python
    AlarmRule.objects.create(
        code="scripted", name="Scripted", category="project", component_type="project",
        default_severity=Severity.HIGH,
        # Los tests legacy prueban el flujo básico del engine sin el gate de
        # confirmación; los tests de confirmación lo suben por-test.
        default_params={"confirmation_cycles": 1},
    )
```

- [ ] **Step 2: Escribir los tests de confirmación (fallan)**

Agregar al final de `apps/alarms/tests/test_engine.py`:

```python
def set_confirmation_cycles(value):
    AlarmRule.objects.filter(code="scripted").update(
        default_params={"confirmation_cycles": value}
    )


@pytest.mark.django_db
class TestConfirmation:
    """Spec 2026-07-11: la alarma nace pending y solo notifica al confirmar."""

    def test_first_firing_creates_pending_without_notification(self, project, scripted_rule):
        set_confirmation_cycles(3)
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        notifier = MagicMock()

        run(project, notifier=notifier)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.PENDING
        assert alarm.confirmed_at is None
        notifier.assert_not_called()

    def test_confirms_and_notifies_once_at_n_cycles(self, project, scripted_rule):
        set_confirmation_cycles(3)
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        notifier = MagicMock()

        run(project, notifier=notifier)
        run(project, notifier=notifier)
        assert Alarm.objects.get().status == Alarm.Status.PENDING
        notifier.assert_not_called()

        run(project, notifier=notifier)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.ACTIVE
        assert alarm.confirmed_at is not None
        assert alarm.occurrence_count == 3
        notifier.assert_called_once_with(alarm, "opened")

    def test_cycles_of_one_opens_active_and_notifies_immediately(self, project, scripted_rule):
        set_confirmation_cycles(1)
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        notifier = MagicMock()

        run(project, notifier=notifier)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.ACTIVE
        assert alarm.confirmed_at is not None
        notifier.assert_called_once_with(alarm, "opened")

    def test_default_without_param_is_three_cycles(self, project, scripted_rule):
        AlarmRule.objects.filter(code="scripted").update(default_params={})
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        notifier = MagicMock()

        run(project, notifier=notifier)

        assert Alarm.objects.get().status == Alarm.Status.PENDING
        notifier.assert_not_called()

    def test_not_computable_neither_confirms_nor_discards(self, project, scripted_rule):
        set_confirmation_cycles(2)
        notifier = MagicMock()
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        run(project, notifier=notifier)

        scripted_rule.outcomes = [RuleOutcome(status="not_computable", reason="poa:no_verificable")]
        run(project, notifier=notifier)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.PENDING
        assert alarm.occurrence_count == 1
        notifier.assert_not_called()

    def test_stats_track_pending_lifecycle(self, project, scripted_rule):
        set_confirmation_cycles(2)
        scripted_rule.outcomes = [RuleOutcome(status="firing")]

        first = run(project)
        second = run(project)

        assert first.stats["pending_opened"] == 1
        assert second.stats["confirmed"] == 1
        assert second.stats["opened"] == 0
```

- [ ] **Step 3: Verificar que fallan**

Run: `.venv/bin/pytest apps/alarms/tests/test_engine.py::TestConfirmation -q`
Expected: FAIL — los primeros tests reportan `status == "active"` en vez de `"pending"` y llamadas inesperadas al notifier.

- [ ] **Step 4: Implementar la rama firing en `_process_outcome`**

En `apps/alarms/engine.py:54`, cambiar la línea de stats por:

```python
    stats = {
        "opened": 0, "pending_opened": 0, "confirmed": 0, "pending_discarded": 0,
        "updated": 0, "resolved": 0, "not_computable": [], "errors": {},
    }
```

En `apps/alarms/tasks.py` (dentro de `check_sla`), cambiar la línea de stats por la misma estructura:

```python
    stats = {
        "opened": 0, "pending_opened": 0, "confirmed": 0, "pending_discarded": 0,
        "updated": 0, "resolved": 0, "not_computable": [], "errors": {},
    }
```

En `apps/alarms/engine.py`, reemplazar la rama `if outcome.status == "firing":` completa (líneas 159-196) por:

```python
    if outcome.status == "firing":
        severity = outcome.severity or rule.default_severity
        # check_sla llama con ctx=None: resolver params sin el cache del contexto
        params = ctx.params(rule.code) if ctx is not None else rule.params_for(project)
        confirmation_cycles = params.get("confirmation_cycles", 3)

        if alarm is None:
            inverter = None
            if outcome.inverter_external_id is not None:
                inverter = Inverter.objects.filter(
                    project=project, external_id=outcome.inverter_external_id
                ).first()
            confirmed = confirmation_cycles <= 1
            alarm = Alarm.objects.create(
                rule=rule,
                project=project,
                inverter=inverter,
                component_type=rule.component_type,
                component_id=outcome.component_id,
                severity=severity,
                status=Alarm.Status.ACTIVE if confirmed else Alarm.Status.PENDING,
                dedup_key=dedup_key,
                triggered_at=now,
                last_seen_at=now,
                confirmed_at=now if confirmed else None,
                evidence=outcome.evidence,
                last_evidence=outcome.evidence,
            )
            if confirmed:
                stats["opened"] += 1
                _notify(notifier, alarm, "opened")
            else:
                stats["pending_opened"] += 1
        else:
            alarm.last_seen_at = now
            alarm.last_evidence = outcome.evidence
            alarm.occurrence_count += 1
            escalated = Severity.rank(severity) > Severity.rank(alarm.severity)
            if escalated:
                alarm.severity = severity
            update_fields = [
                "last_seen_at", "last_evidence", "occurrence_count", "severity",
            ]
            confirming = (
                alarm.status == Alarm.Status.PENDING
                and alarm.occurrence_count >= confirmation_cycles
            )
            if confirming:
                alarm.status = Alarm.Status.ACTIVE
                alarm.confirmed_at = now
                update_fields += ["status", "confirmed_at"]
            alarm.save(update_fields=update_fields)
            if confirming:
                stats["confirmed"] += 1
                # Primera notificación de esta alarma: sale al confirmar, con la
                # severidad vigente (una escalada durante pending viaja aquí).
                _notify(notifier, alarm, "opened")
            elif alarm.status == Alarm.Status.PENDING:
                stats["updated"] += 1  # pending juntando ciclos: silencio
            else:
                stats["updated"] += 1
                if escalated:
                    _notify(notifier, alarm, "escalated")
```

- [ ] **Step 5: Verificar que los tests nuevos pasan**

Run: `.venv/bin/pytest apps/alarms/tests/test_engine.py -q`
Expected: PASS completo (legacy + TestConfirmation).

- [ ] **Step 6: Actualizar test_check_sla — los breaches ahora nacen pending**

Run: `.venv/bin/pytest apps/alarms/tests/test_check_sla.py -q`
Expected: FAIL en los tests que asumen breach `active` tras un solo `check_sla()`.

En `apps/alarms/tests/test_check_sla.py`, actualizar `test_stale_active_alarm_opens_breach`:

```python
    def test_stale_active_alarm_opens_breach(self, project):
        source = make_source_alarm(project, age_minutes=90)  # SLA default 60

        check_sla()

        breach = breaches().get()
        # El breach también pasa por el gate de confirmación (default 3)
        assert breach.status == Alarm.Status.PENDING

        check_sla()
        check_sla()

        breach.refresh_from_db()
        assert breach.status == Alarm.Status.ACTIVE
        assert breach.severity == Severity.HIGH
        assert breach.dedup_key == f"alarm_sla_breach:146:alarm:{source.id}"
        assert breach.evidence["source_rule"] == "weather_comm_lost"
```

Aplicar el mismo patrón (llamar `check_sla()` 3 veces antes de asertar `ACTIVE`) en cualquier otro test del archivo que aserte el estado o la notificación del breach. Los tests que asertan `count() == 0` no cambian.

- [ ] **Step 7: Suite del módulo en verde**

Run: `.venv/bin/pytest apps/alarms/tests/ -q`
Expected: PASS completo.

- [ ] **Step 8: Commit**

```bash
git add apps/alarms/engine.py apps/alarms/tasks.py apps/alarms/tests/test_engine.py apps/alarms/tests/test_check_sla.py
git commit -m "feat: gate de confirmación — alarmas nacen pending y notifican al ciclo N"
```

---

### Task 3: Engine — `ok` descarta pending en silencio; escalada pending silenciosa

**Files:**
- Modify: `apps/alarms/tests/test_engine.py` (TestConfirmation)
- Modify: `apps/alarms/engine.py:198-204` (rama ok)

- [ ] **Step 1: Escribir los tests (fallan)**

Agregar a `TestConfirmation` en `apps/alarms/tests/test_engine.py`:

```python
    def test_ok_discards_pending_silently(self, project, scripted_rule):
        set_confirmation_cycles(3)
        notifier = MagicMock()
        scripted_rule.outcomes = [RuleOutcome(status="firing")]
        run(project, notifier=notifier)

        scripted_rule.outcomes = [RuleOutcome(status="ok")]
        result = run(project, notifier=notifier)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.RESOLVED
        assert alarm.resolution_type == Alarm.ResolutionType.AUTO
        notifier.assert_not_called()
        assert result.stats["pending_discarded"] == 1
        assert result.stats["resolved"] == 0

    def test_escalation_while_pending_is_silent_and_carries_final_severity(
        self, project, scripted_rule
    ):
        set_confirmation_cycles(3)
        notifier = MagicMock()
        scripted_rule.outcomes = [RuleOutcome(status="firing", severity=Severity.HIGH)]
        run(project, notifier=notifier)

        scripted_rule.outcomes = [RuleOutcome(status="firing", severity=Severity.CRITICAL)]
        run(project, notifier=notifier)
        notifier.assert_not_called()  # sin evento "escalated" durante pending

        run(project, notifier=notifier)

        alarm = Alarm.objects.get()
        assert alarm.status == Alarm.Status.ACTIVE
        assert alarm.severity == Severity.CRITICAL
        notifier.assert_called_once_with(alarm, "opened")
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/pytest apps/alarms/tests/test_engine.py::TestConfirmation -q`
Expected: FAIL — `test_ok_discards_pending_silently` recibe una llamada `resolved` en el notifier (la rama ok aún notifica todo). El de escalada puede pasar ya (la rama firing del Task 2 lo cubre); si pasa, déjalo como test de regresión.

- [ ] **Step 3: Implementar la rama ok**

En `apps/alarms/engine.py`, reemplazar la rama `elif outcome.status == "ok" ...` por:

```python
    elif outcome.status == "ok" and alarm is not None and rule.auto_resolve:
        was_pending = alarm.status == Alarm.Status.PENDING
        alarm.status = Alarm.Status.RESOLVED
        alarm.resolved_at = now
        alarm.resolution_type = Alarm.ResolutionType.AUTO
        alarm.save(update_fields=["status", "resolved_at", "resolution_type"])
        if was_pending:
            # Falso positivo de 1-2 ciclos: muere sin haber hecho ruido.
            stats["pending_discarded"] += 1
        else:
            stats["resolved"] += 1
            _notify(notifier, alarm, "resolved")
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/pytest apps/alarms/tests/ -q`
Expected: PASS completo.

- [ ] **Step 5: Commit**

```bash
git add apps/alarms/engine.py apps/alarms/tests/test_engine.py
git commit -m "feat: pending descartada por ok se resuelve en silencio"
```

---

### Task 4: recloser_open exento (confirmation_cycles=1)

**Files:**
- Modify: `apps/alarms/catalog.py:260` (default_params de recloser_open)
- Create: `apps/alarms/migrations/00XX_recloser_confirmation_cycles.py`
- Modify: `apps/alarms/tests/test_catalog.py`

- [ ] **Step 1: Escribir el test del catálogo (falla)**

Agregar a `apps/alarms/tests/test_catalog.py`:

```python
@pytest.mark.django_db
def test_recloser_open_confirms_immediately():
    """Spec 2026-07-11: una desconexión de red no espera el gate de confirmación."""
    from apps.alarms.models import AlarmRule

    rule = AlarmRule.objects.get(code="recloser_open")
    assert rule.default_params["confirmation_cycles"] == 1
```

(Si el archivo no importa `pytest`, agregar `import pytest` arriba.)

Run: `.venv/bin/pytest apps/alarms/tests/test_catalog.py -q`
Expected: FAIL con `KeyError: 'confirmation_cycles'`.

- [ ] **Step 2: Actualizar el catálogo**

En `apps/alarms/catalog.py`, en la entrada `recloser_open`, cambiar:

```python
        "default_params": {"solar_margin_minutes": 30},
```

por:

```python
        # confirmation_cycles=1: la alerta de red más crítica no espera el gate
        # de confirmación (decisión del usuario, spec 2026-07-11).
        "default_params": {"solar_margin_minutes": 30, "confirmation_cycles": 1},
```

- [ ] **Step 3: Data migration para la fila ya seedeada**

Run: `.venv/bin/python manage.py makemigrations alarms --empty -n recloser_confirmation_cycles`

Editar el archivo generado:

```python
from django.db import migrations


def add_param(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="recloser_open").first()
    if rule is not None:
        rule.default_params = {**rule.default_params, "confirmation_cycles": 1}
        rule.save(update_fields=["default_params"])


def remove_param(apps, schema_editor):
    AlarmRule = apps.get_model("alarms", "AlarmRule")
    rule = AlarmRule.objects.filter(code="recloser_open").first()
    if rule is not None:
        rule.default_params.pop("confirmation_cycles", None)
        rule.save(update_fields=["default_params"])


class Migration(migrations.Migration):

    dependencies = [
        ("alarms", "00XX_backfill_confirmed_at"),  # ← ajustar al nombre real
    ]

    operations = [
        migrations.RunPython(add_param, remove_param),
    ]
```

- [ ] **Step 4: Migrar y verificar**

Run: `.venv/bin/python manage.py migrate alarms && .venv/bin/pytest apps/alarms/tests/test_catalog.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alarms/catalog.py apps/alarms/migrations/ apps/alarms/tests/test_catalog.py
git commit -m "feat: recloser_open exento del gate de confirmación (N=1)"
```

---

### Task 5: check_sla cuenta desde confirmed_at e ignora pending

**Files:**
- Modify: `apps/alarms/tests/test_check_sla.py`
- Modify: `apps/alarms/tasks.py:69` (cálculo de edad)

- [ ] **Step 1: Escribir los tests (fallan)**

En `apps/alarms/tests/test_check_sla.py`, actualizar `make_source_alarm` para aceptar `confirmed_at`:

```python
def make_source_alarm(project, age_minutes, status=Alarm.Status.ACTIVE,
                      confirmed_age_minutes=None):
    rule = AlarmRule.objects.get(code="weather_comm_lost")
    triggered = timezone.now() - timedelta(minutes=age_minutes)
    confirmed = (
        timezone.now() - timedelta(minutes=confirmed_age_minutes)
        if confirmed_age_minutes is not None
        else triggered
    )
    return Alarm.objects.create(
        rule=rule, project=project, component_type=rule.component_type,
        severity=rule.default_severity,
        dedup_key=Alarm.build_dedup_key(rule.code, project.external_id),
        triggered_at=triggered, last_seen_at=triggered,
        confirmed_at=confirmed, status=status,
    )
```

Y agregar a `TestCheckSla`:

```python
    def test_sla_clock_runs_from_confirmed_at_not_triggered_at(self, project):
        # Disparó hace 90 min pero se confirmó hace 30: aún dentro del SLA de 60
        make_source_alarm(project, age_minutes=90, confirmed_age_minutes=30)

        check_sla()

        assert breaches().count() == 0

    def test_pending_alarm_never_breaches(self, project):
        make_source_alarm(project, age_minutes=90, status=Alarm.Status.PENDING)

        check_sla()

        assert breaches().count() == 0
```

- [ ] **Step 2: Verificar que falla el primero**

Run: `.venv/bin/pytest apps/alarms/tests/test_check_sla.py -q`
Expected: FAIL en `test_sla_clock_runs_from_confirmed_at_not_triggered_at` (abre breach usando triggered_at). `test_pending_alarm_never_breaches` pasa ya (el filtro `status=ACTIVE` excluye pending) — queda como regresión.

- [ ] **Step 3: Implementar**

En `apps/alarms/tasks.py`, dentro de `check_sla`, cambiar:

```python
        age_minutes = (now - alarm.triggered_at).total_seconds() / 60
```

por:

```python
        # SLA desde la confirmación (= la notificación que el equipo pudo ver);
        # fallback a triggered_at para filas anteriores al backfill.
        age_minutes = (now - (alarm.confirmed_at or alarm.triggered_at)).total_seconds() / 60
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/pytest apps/alarms/tests/test_check_sla.py -q`
Expected: PASS completo.

- [ ] **Step 5: Commit**

```bash
git add apps/alarms/tasks.py apps/alarms/tests/test_check_sla.py
git commit -m "feat: SLA cuenta desde confirmed_at e ignora pending"
```

---

### Task 6: Admin, suite completa y cierre

**Files:**
- Modify: `apps/alarms/admin.py:22-29`

- [ ] **Step 1: Exponer confirmed_at en el admin**

En `apps/alarms/admin.py`, en `AlarmAdmin`, cambiar:

```python
    list_display = (
        "dedup_key", "severity", "status", "project", "triggered_at",
        "last_seen_at", "occurrence_count",
    )
```

por:

```python
    list_display = (
        "dedup_key", "severity", "status", "project", "triggered_at",
        "confirmed_at", "last_seen_at", "occurrence_count",
    )
```

(El filtro de `status` existente ya muestra `pending`; la acción `acknowledge` filtra `status=ACTIVE`, así que una pending no puede reconocerse — correcto según spec.)

- [ ] **Step 2: Suite completa del proyecto**

Run: `.venv/bin/pytest -q`
Expected: PASS completo (todas las apps, no solo alarms).

- [ ] **Step 3: Verificación en vivo (opcional pero recomendada)**

Con el worker corriendo, esperar 2-3 ciclos y revisar:

```bash
.venv/bin/python manage.py shell -c "
from apps.alarms.models import Alarm
from django.db.models import Count
for r in Alarm.objects.values('status').annotate(c=Count('id')): print(r)"
```

Expected: aparecen filas `pending`; ninguna notificación de Discord para pendings que mueren solas.

- [ ] **Step 4: Commit final**

```bash
git add apps/alarms/admin.py
git commit -m "feat: confirmed_at visible en admin de alarmas"
```
