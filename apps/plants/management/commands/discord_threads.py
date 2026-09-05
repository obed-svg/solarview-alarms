"""Hilos de Discord por proyecto (T49).

Sin argumentos lista los proyectos con alarmas y si ya tienen hilo — la lista
de hilos a crear en el canal del webhook. Con `--set` carga los IDs en lote:

    python manage.py discord_threads
    python manage.py discord_threads --set 146=1234567890 158=1234567891
"""

from django.core.management.base import BaseCommand, CommandError

from apps.plants.models import Project


class Command(BaseCommand):
    help = "Lista los proyectos con alarmas y sus hilos de Discord; --set los asigna"

    def add_arguments(self, parser):
        parser.add_argument(
            "--set",
            nargs="+",
            metavar="EXTERNAL_ID=THREAD_ID",
            default=[],
            help="Asigna hilos en lote. THREAD_ID vacío borra el hilo del proyecto.",
        )
        parser.add_argument(
            "--pending",
            action="store_true",
            help="Solo los proyectos con alarmas que aún no tienen hilo",
        )

    def handle(self, *args, **options):
        if options["set"]:
            self._assign(options["set"])

        projects = Project.objects.alarmable().order_by("name")
        if options["pending"]:
            projects = projects.filter(discord_thread_id="")

        for project in projects:
            thread = project.discord_thread_id or "— SIN HILO —"
            self.stdout.write(f"{project.external_id}\t{project.name}\t{thread}")

        total = Project.objects.alarmable().count()
        pending = Project.objects.alarmable().filter(discord_thread_id="").count()
        excluded = Project.objects.exclude(
            id__in=Project.objects.alarmable().values("id")
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{total} proyectos con alarmas ({pending} sin hilo), "
                f"{excluded} excluidos (sin monitoreo o degradados a no-minigranja)"
            )
        )

    def _assign(self, pairs: list[str]) -> None:
        for pair in pairs:
            if "=" not in pair:
                raise CommandError(f"Formato inválido {pair!r}: se espera EXTERNAL_ID=THREAD_ID")
            external_id, thread_id = pair.split("=", 1)
            updated = Project.objects.filter(external_id=external_id.strip()).update(
                discord_thread_id=thread_id.strip()
            )
            if not updated:
                raise CommandError(f"No existe el proyecto con external_id={external_id}")
            thread = thread_id.strip() or "(sin hilo)"
            self.stdout.write(f"proyecto {external_id.strip()} → hilo {thread}")
