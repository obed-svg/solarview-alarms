import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

ID_OPTION = [{"type": 4, "name": "id", "description": "ID de alarma", "required": True}]
COMMANDS = [
    {
        "name": "alarma",
        "description": "Consulta u opera una alarma",
        "options": [
            {"type": 1, "name": "ver", "description": "Ver una alarma", "options": ID_OPTION},
            {
                "type": 1,
                "name": "reconocer",
                "description": "Reconocer una alarma",
                "options": ID_OPTION,
            },
            {
                "type": 1,
                "name": "resolver",
                "description": "Resolver una alarma",
                "options": ID_OPTION,
            },
            {
                "type": 1,
                "name": "excluir",
                "description": "Excluir esta regla para el proyecto",
                "options": ID_OPTION,
            },
            {
                "type": 1,
                "name": "incluir",
                "description": "Volver a habilitar esta regla para el proyecto",
                "options": ID_OPTION,
            },
        ],
    },
    {
        "name": "resumen",
        "description": "Muestra el resumen general de alarmas abiertas",
        "options": [
            {
                "type": 3,
                "name": "proyecto",
                "description": "Proyecto específico (escribe una parte del nombre)",
                "required": False,
                "autocomplete": True,
            },
            {
                "type": 4,
                "name": "pagina",
                "description": "Página del resumen",
                "required": False,
                "min_value": 1,
            },
        ],
    },
]


class Command(BaseCommand):
    help = "Sincroniza /alarma en el servidor Discord configurado."

    def handle(self, *args, **options):
        required = (
            settings.DISCORD_APPLICATION_ID,
            settings.DISCORD_GUILD_ID,
            settings.DISCORD_BOT_TOKEN,
        )
        if not all(required):
            raise CommandError("Falta configuración de Discord en .env")
        url = (
            "https://discord.com/api/v10/applications/"
            f"{settings.DISCORD_APPLICATION_ID}/guilds/{settings.DISCORD_GUILD_ID}/commands"
        )
        response = requests.put(
            url,
            json=COMMANDS,
            headers={"Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}"},
            timeout=15,
        )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CommandError(f"Discord rechazó la sincronización: {response.text[:300]}") from exc
        self.stdout.write(self.style.SUCCESS("Comandos de Discord sincronizados."))
