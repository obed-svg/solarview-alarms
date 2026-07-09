"""Motor de evaluación: orquesta las reglas de un proyecto en un tick.

Semántica tri-estado por outcome:
- firing → upsert (crear alarma nueva o actualizar la abierta; nunca duplicar)
- ok → resolver de inmediato la alarma abierta (decisión de producto: el usuario
  ve la corrección al instante; sin histéresis)
- not_computable → no tocar nada

Las reglas corren en 3 fases (comunicación → calidad de datos → eléctricas) para
que las exclusiones consulten ctx.flag_active().
"""

import logging
from collections import Counter

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone as dj_timezone

from apps.plants.models import Inverter, InverterStateObservation
from integrations.solarview.client import SolarViewClient

from .context import EvaluationContext, Unavailable
from .models import Alarm, AlarmRule, EvaluationRun, Severity
from .rules import base as rules_base
from .rules.base import RULES, RuleOutcome

logger = logging.getLogger(__name__)

# Reglas del catálogo que NO corren en el engine (tienen su propio task):
NON_ENGINE_RULES = {"alarm_sla_breach"}


def evaluate_project(
    project,
    client: SolarViewClient | None = None,
    now=None,
    rule_group: str = "fast",
    notifier=None,
) -> EvaluationRun:
    """Evalúa todas las reglas habilitadas del grupo para un proyecto.

    `notifier(alarm, event)` se invoca en opened/escalated/resolved (lo conecta
    el dispatcher de notifications; None = sin notificaciones).
    """
    client = client or SolarViewClient.from_settings()
    ctx = EvaluationContext(project=project, client=client, now=now)
    run = EvaluationRun.objects.create(
        project=project,
        rule_group=rule_group,
        started_at=dj_timezone.now(),
        status=EvaluationRun.Status.SUCCESS,
    )
    stats = {"opened": 0, "updated": 0, "resolved": 0, "not_computable": [], "errors": {}}

    db_rules = [
        rule
        for rule in AlarmRule.objects.filter(rule_group=rule_group).exclude(
            code__in=NON_ENGINE_RULES
        )
        if rule.code in RULES and rule.is_enabled_for(project)
    ]
    db_rules.sort(key=lambda rule: RULES[rule.code].phase)

    for rule in db_rules:
        try:
            outcomes = RULES[rule.code]().evaluate(ctx)
        except Exception:
            logger.exception("Regla %s explotó en proyecto %s", rule.code, project.external_id)
            stats["errors"][rule.code] = "exception"
            run.status = EvaluationRun.Status.PARTIAL
            continue

        for outcome in outcomes:
            if outcome.status == "firing":
                ctx.set_firing(rule.code, outcome.dedup_suffix)
            try:
                _process_outcome(rule, project, outcome, ctx, stats, notifier)
            except Exception:
                logger.exception(
                    "Procesando outcome de %s en proyecto %s", rule.code, project.external_id
                )
                stats["errors"][rule.code] = "outcome_processing"
                run.status = EvaluationRun.Status.PARTIAL

    try:
        _record_state_observations(ctx)
    except Exception:
        # el censo es observabilidad, nunca degrada el run
        logger.exception("Censo de estados falló en proyecto %s", project.external_id)

    run.finished_at = dj_timezone.now()
    run.stats = stats
    run.save(update_fields=["finished_at", "stats", "status"])
    return run


def _record_state_observations(ctx: EvaluationContext) -> None:
    """T30: censo pasivo del vocabulario de `state` (Huawei SUN2000, ver
    InverterStateObservation). Reusa los inversores que las reglas ya
    consultaron en este tick — si ninguna regla los pidió o la API falló,
    no hace nada. Cero requests extra."""
    inverters = ctx._cache.get("inverters")
    if not inverters or isinstance(inverters, Unavailable):
        return

    counts = Counter(inv.state for inv in inverters if inv.state)
    now = dj_timezone.now()
    for state, n in counts.items():
        updated = InverterStateObservation.objects.filter(state=state).update(
            times_seen=F("times_seen") + n, last_seen_at=now
        )
        if updated:
            continue
        first = next(inv for inv in inverters if inv.state == state)
        try:
            with transaction.atomic():  # savepoint: la carrera no rompe el tick
                InverterStateObservation.objects.create(
                    state=state,
                    last_seen_at=now,
                    times_seen=n,
                    first_project=ctx.project,
                    first_dev_name=first.dev_name or "",
                )
            logger.info(
                "Estado de inversor NUEVO observado: %r (proyecto %s, %s)",
                state, ctx.project.external_id, first.dev_name,
            )
        except IntegrityError:
            # otro worker lo creó en paralelo: contar este avistamiento igual
            InverterStateObservation.objects.filter(state=state).update(
                times_seen=F("times_seen") + n, last_seen_at=now
            )


def _notify(notifier, alarm: Alarm, event: str) -> None:
    if notifier is not None:
        notifier(alarm, event)


def _process_outcome(rule, project, outcome: RuleOutcome, ctx, stats, notifier) -> None:
    dedup_key = Alarm.build_dedup_key(rule.code, project.external_id, outcome.dedup_suffix)
    now = dj_timezone.now()

    if outcome.status == "not_computable":
        stats["not_computable"].append(
            {"rule": rule.code, "suffix": outcome.dedup_suffix, "reason": outcome.reason}
        )
        return

    with transaction.atomic():
        alarm = (
            Alarm.objects.select_for_update()
            .filter(dedup_key=dedup_key)
            .exclude(status=Alarm.Status.RESOLVED)
            .first()
        )

        if outcome.status == "firing":
            severity = outcome.severity or rule.default_severity
            if alarm is None:
                inverter = None
                if outcome.inverter_external_id is not None:
                    inverter = Inverter.objects.filter(
                        project=project, external_id=outcome.inverter_external_id
                    ).first()
                alarm = Alarm.objects.create(
                    rule=rule,
                    project=project,
                    inverter=inverter,
                    component_type=rule.component_type,
                    component_id=outcome.component_id,
                    severity=severity,
                    dedup_key=dedup_key,
                    triggered_at=now,
                    last_seen_at=now,
                    evidence=outcome.evidence,
                    last_evidence=outcome.evidence,
                )
                stats["opened"] += 1
                _notify(notifier, alarm, "opened")
            else:
                alarm.last_seen_at = now
                alarm.last_evidence = outcome.evidence
                alarm.occurrence_count += 1
                escalated = Severity.rank(severity) > Severity.rank(alarm.severity)
                if escalated:
                    alarm.severity = severity
                alarm.save(
                    update_fields=[
                        "last_seen_at", "last_evidence", "occurrence_count", "severity",
                    ]
                )
                stats["updated"] += 1
                if escalated:
                    _notify(notifier, alarm, "escalated")

        elif outcome.status == "ok" and alarm is not None and rule.auto_resolve:
            alarm.status = Alarm.Status.RESOLVED
            alarm.resolved_at = now
            alarm.resolution_type = Alarm.ResolutionType.AUTO
            alarm.save(update_fields=["status", "resolved_at", "resolution_type"])
            stats["resolved"] += 1
            _notify(notifier, alarm, "resolved")


def validate_registry() -> list[str]:
    """Códigos del catálogo (enabled o no) sin clase registrada — para arranque/CI."""
    expected = set(
        AlarmRule.objects.exclude(code__in=NON_ENGINE_RULES).values_list("code", flat=True)
    )
    return sorted(expected - set(rules_base.RULES))
