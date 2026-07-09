from django.db import models
from django.db.models import Q


class Project(models.Model):
    """Espejo local de un proyecto de la API SolarView (sincronizado por sync_catalog)."""

    external_id = models.PositiveIntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=200)
    plant_code = models.CharField(max_length=100, blank=True, default="")
    weather_plant_code = models.CharField(max_length=100, blank=True, default="")
    installed_capacity_kw = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    timezone = models.CharField(max_length=64, default="America/Bogota")
    is_minifarm = models.BooleanField(default=False)
    monitoring_enabled = models.BooleanField(default=True)
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField()

    def __str__(self):
        return f"{self.name} (#{self.external_id})"


class Inverter(models.Model):
    """Espejo local de un inversor. No se borra al desaparecer de la API: se inactiva."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="inverters")
    external_id = models.PositiveIntegerField()
    dev_name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    raw = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "external_id"], name="uniq_inverter_per_project"
            ),
        ]

    def __str__(self):
        return f"{self.dev_name} (#{self.external_id})"


class MaintenanceWindowQuerySet(models.QuerySet):
    def active_at(self, project, at, inverter=None):
        """Ventanas vigentes en `at` para el proyecto.

        Sin `inverter`: solo ventanas de proyecto completo (inverter IS NULL).
        Con `inverter`: ventanas de ese inversor O de proyecto completo.
        """
        qs = self.filter(project=project, starts_at__lte=at, ends_at__gte=at)
        if inverter is None:
            return qs.filter(inverter__isnull=True)
        return qs.filter(Q(inverter__isnull=True) | Q(inverter=inverter))


class MaintenanceWindow(models.Model):
    """Exclusión "mantenimiento registrado" que piden varias alarmas. Se gestiona por admin."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="maintenance_windows"
    )
    inverter = models.ForeignKey(
        Inverter, on_delete=models.CASCADE, null=True, blank=True,
        help_text="Vacío = aplica a todo el proyecto",
    )
    component_type = models.CharField(max_length=20, blank=True, default="")
    component_id = models.CharField(max_length=100, blank=True, default="")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.TextField(blank=True, default="")
    created_by = models.EmailField(blank=True, default="")

    objects = MaintenanceWindowQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["project", "starts_at", "ends_at"])]

    def __str__(self):
        scope = self.inverter or "proyecto"
        return f"Mantenimiento {self.project_id}/{scope}: {self.starts_at} - {self.ends_at}"


class InverterStateObservation(models.Model):
    """Censo pasivo del vocabulario de `state` de los inversores (T30).

    El backend consulta inversores Huawei SUN2000: `state` debería ser el enum
    "Device status" (registro Modbus 32089) en texto, pero la redacción exacta
    que entrega SolarView está POR CONFIRMAR (T31) — solo se han observado
    "Grid-connected" y "Standby: insulation resistance detecting". Cada tick
    del engine anota aquí los estados vistos (reusa los inversores que las
    reglas ya consultaron: cero requests extra). Cuando aparezca un estado
    nuevo (derating, falla), queda registrado con fecha, proyecto e inversor
    para ajustar los keywords de las reglas 3/7 con evidencia.
    """

    state = models.CharField(max_length=255, unique=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    times_seen = models.PositiveIntegerField(
        default=1, help_text="Avistamientos acumulados (inversor × tick)"
    )
    first_project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Proyecto donde se observó por primera vez",
    )
    first_dev_name = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self):
        return self.state
