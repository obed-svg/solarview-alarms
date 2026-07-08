from django.db import models
from django.db.models import Q


class Severity(models.TextChoices):
    CRITICAL = "critical", "Crítica"
    HIGH = "high", "Alta"
    MEDIUM = "medium", "Media"
    LOW = "low", "Baja"

    @staticmethod
    def rank(value: str) -> int:
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(value)


class Category(models.TextChoices):
    PROJECT = "project", "Proyecto"
    INVERTER = "inverter", "Inversor"
    DC_STRING = "dc_string", "DC / strings"
    METER = "meter", "Medidores"
    DATA = "data", "Datos"
    WEATHER = "weather", "Meteo"
    GRID = "grid", "Red / MT"
    POWER_QUALITY = "power_quality", "Calidad energía"
    OM = "om", "Gestión O&M"


class ComponentType(models.TextChoices):
    PROJECT = "project", "Proyecto"
    INVERTER = "inverter", "Inversor"
    STRING = "string", "String"
    METER = "meter", "Medidor"
    WEATHER_STATION = "weather_station", "Estación meteo"
    RELAY = "relay", "Reconectador"


class RuleGroup(models.TextChoices):
    FAST = "fast", "Cada 5 min"
    HOURLY = "hourly", "Cada hora"


class AlarmRule(models.Model):
    """Catálogo: una fila por tipo de alarma. La lógica vive en apps/alarms/rules/
    unida por `code`; los umbrales por defecto (COX) viven en default_params."""

    code = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=20, choices=Category.choices)
    component_type = models.CharField(max_length=20, choices=ComponentType.choices)
    default_severity = models.CharField(max_length=10, choices=Severity.choices)
    enabled = models.BooleanField(default=True)
    default_params = models.JSONField(default=dict, blank=True)
    rule_group = models.CharField(
        max_length=10, choices=RuleGroup.choices, default=RuleGroup.FAST
    )
    auto_resolve = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} ({self.get_default_severity_display()})"

    def params_for(self, project) -> dict:
        """default_params del catálogo pisados por el override del proyecto (si existe)."""
        config = self.configs.filter(project=project).first()
        merged = dict(self.default_params)
        if config:
            merged.update(config.params)
        return merged

    def is_enabled_for(self, project) -> bool:
        """enabled del catálogo, salvo override no-nulo en RuleConfig."""
        config = self.configs.filter(project=project).first()
        if config and config.enabled is not None:
            return config.enabled
        return self.enabled


class RuleConfig(models.Model):
    """Override POR proyecto: solo las claves declaradas en params pisan el catálogo."""

    rule = models.ForeignKey(AlarmRule, on_delete=models.CASCADE, related_name="configs")
    project = models.ForeignKey(
        "plants.Project", on_delete=models.CASCADE, related_name="rule_configs"
    )
    enabled = models.BooleanField(null=True, blank=True, help_text="Vacío = hereda del catálogo")
    params = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rule", "project"], name="uniq_ruleconfig"),
        ]

    def __str__(self):
        return f"{self.rule.code} @ {self.project}"


class Alarm(models.Model):
    """Una alarma concreta. Máximo UNA abierta por dedup_key (constraint parcial)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Activa"
        ACKNOWLEDGED = "acknowledged", "Reconocida"
        RESOLVED = "resolved", "Resuelta"

    class ResolutionType(models.TextChoices):
        AUTO = "auto", "Automática"
        MANUAL = "manual", "Manual"

    rule = models.ForeignKey(AlarmRule, on_delete=models.PROTECT, related_name="alarms")
    project = models.ForeignKey(
        "plants.Project", on_delete=models.PROTECT, related_name="alarms"
    )
    inverter = models.ForeignKey(
        "plants.Inverter", on_delete=models.PROTECT, null=True, blank=True,
        related_name="alarms",
    )
    component_type = models.CharField(max_length=20, choices=ComponentType.choices)
    component_id = models.CharField(max_length=100, blank=True, default="")
    severity = models.CharField(max_length=10, choices=Severity.choices)
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    dedup_key = models.CharField(max_length=255)
    triggered_at = models.DateTimeField(db_index=True)
    last_seen_at = models.DateTimeField()
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.EmailField(blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_type = models.CharField(
        max_length=10, choices=ResolutionType.choices, blank=True, default=""
    )
    evidence = models.JSONField(default=dict, blank=True)
    last_evidence = models.JSONField(default=dict, blank=True)
    occurrence_count = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dedup_key"],
                condition=~Q(status="resolved"),
                name="uniq_open_alarm_per_dedup_key",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["rule", "status"]),
            models.Index(fields=["status", "severity", "triggered_at"]),
        ]

    def __str__(self):
        return f"[{self.get_severity_display()}] {self.dedup_key} ({self.status})"

    @staticmethod
    def build_dedup_key(rule_code: str, project_external_id: int, *suffixes: str) -> str:
        parts = [rule_code, str(project_external_id), *[s for s in suffixes if s]]
        return ":".join(parts)


class NonComputableInterval(models.Model):
    """Intervalos donde PR o disponibilidad NO son calculables por falta de datos.
    El requerimiento prohíbe inventar 0%/100%: esto es la marca explícita."""

    class Metric(models.TextChoices):
        PR = "pr", "Performance ratio"
        AVAILABILITY = "availability", "Disponibilidad"

    project = models.ForeignKey("plants.Project", on_delete=models.CASCADE)
    inverter = models.ForeignKey(
        "plants.Inverter", on_delete=models.CASCADE, null=True, blank=True
    )
    metric = models.CharField(max_length=20, choices=Metric.choices)
    interval_start = models.DateTimeField()
    interval_end = models.DateTimeField()
    missing_inputs = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            # dos constraints parciales en vez de nulls_distinct=False (solo postgres>=15):
            models.UniqueConstraint(
                fields=["project", "inverter", "metric", "interval_start"],
                condition=Q(inverter__isnull=False),
                name="uniq_noncomputable_inverter",
            ),
            models.UniqueConstraint(
                fields=["project", "metric", "interval_start"],
                condition=Q(inverter__isnull=True),
                name="uniq_noncomputable_project",
            ),
        ]

    def __str__(self):
        return f"{self.metric} no calculable {self.project_id} @ {self.interval_start}"


class EvaluationRun(models.Model):
    """Bitácora de cada tick de evaluación por proyecto (observabilidad en admin)."""

    class Status(models.TextChoices):
        SUCCESS = "success", "OK"
        PARTIAL = "partial", "Parcial (alguna regla falló)"
        FAILED = "failed", "Falló"

    project = models.ForeignKey("plants.Project", on_delete=models.CASCADE)
    rule_group = models.CharField(max_length=10, choices=RuleGroup.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    stats = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["project", "-started_at"])]

    def __str__(self):
        return f"Run {self.project_id}/{self.rule_group} {self.started_at} ({self.status})"
