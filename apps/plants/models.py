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
