# solarview-alarms

Sistema de alarmas para plantas solares SolarView: lee datos de la API de
monitoreo v1 (prefijo canónico `/solarview/`), evalúa las alarmas de Fase 1 (19 de
21 activas; THD y SLA interno deshabilitados) cada 5 minutos con Celery, persiste
alarmas deduplicadas en PostgreSQL y notifica por Discord o WhatsApp con
trazabilidad del destino.

## Arquitectura

```
integrations/solarview/   Cliente HTTP tipado (auth Token, retries, excepciones)
apps/plants/              Cache local de proyectos/inversores + ventanas de mantenimiento
apps/alarms/              Catálogo de reglas, engine tri-estado, alarmas, bitácora de runs
apps/notifications/       Discord/WhatsApp + comandos + resumen + log idempotente
config/                   Settings (django-environ), Celery (colas + beat)
```

Documentación:

- `docs/DISENO.md` — diseño completo: modelos campo por campo, motor de
  evaluación (contrato de regla, EvaluationContext, semántica tri-estado),
  mapa alarma→endpoints y decisiones de arquitectura.
- La guía de despliegue en AWS EC2 está en la sección **Despliegue en AWS** de
  este README.
- `docs/API_SOLARVIEW_V1.md` — contrato vigente, mapeo legacy→v1 y smoke test.
- `docs/Documentacion_API_SolarView.md` — conversión Markdown del PDF recibido.
- `ROADMAP.md` — historia de implementación: tareas, gotchas de la API real y
  pendientes que requieren acción del backend (sección Bloqueadas).
- Cada regla documenta su lógica y exclusiones en su docstring
  (`apps/alarms/rules/`); las descripciones del catálogo se ven en el admin.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d          # postgres:16 + redis:7
cp .env.example .env          # y llenar las llaves (ver tabla abajo)
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
```

Llaves del `.env` (nunca commitear):

| Llave | Contenido |
|---|---|
| `SOLARVIEW_BASE_URL` | Host del gateway (default recomendado: `api.sole.tech`) |
| `webhook_discord` | URL del webhook del canal principal de Discord |
| `discord_bot_token` | Token del bot, usado para registrar slash commands |
| `discord_application_id` | ID de la aplicación Discord |
| `discord_public_key` | Clave pública para verificar interacciones |
| `discord_guild_id` | ID del servidor Discord autorizado |
| `static_token` | Token estático de la API SolarView |
| `WHATSAPP_ACCESS_TOKEN` | Token permanente de WhatsApp Cloud API |
| `WHATSAPP_PHONE_NUMBER_ID` | Referencia para cargar el ID en los canales del admin |
| `WHATSAPP_VERIFY_TOKEN` | Token elegido para verificar el webhook |
| `WHATSAPP_APP_SECRET` | App Secret de Meta para validar firmas entrantes |
| `WHATSAPP_API_VERSION` | Opcional, default `v23.0` |
| `DATABASE_URL` | opcional, default postgres local |
| `REDIS_URL` | opcional, default redis local |

## Operación

```bash
# worker + beat (los schedules por defecto ya están seedeados por migración)
.venv/bin/celery -A config worker -B -l info

# admin: UI operativa (alarmas, umbrales por proyecto, canales, schedules, runs)
.venv/bin/python manage.py runserver
```

- Los schedules (evaluación cada 5 min, hourly, SLA cada 10 min, sync horario)
  se editan en admin → Periodic tasks.
- Las zonas se crean en admin → **Zones** con nombre y `whatsapp_group_id`; los
  proyectos se asignan a una zona desde la lista de **Projects** o dentro de la zona.
- Los canales `ops-whatsapp` y `resumen-whatsapp` nacen **deshabilitados**. En
  admin → **Notification channels**, cargar el mismo `whatsapp_phone_number_id`,
  mantener `WHATSAPP_ACCESS_TOKEN` como `env_key`, completar el
  `destination_group_id` solo en el canal general y habilitarlos.
- El webhook público es `/whatsapp/webhook/`. Meta valida por GET con
  `WHATSAPP_VERIFY_TOKEN`; cada POST se valida con `WHATSAPP_APP_SECRET`.
- Los grupos de zona reciben eventos en tiempo real. Sus comandos son
  `alarma <id> ver`, `alarma <id> reconocer`, `alarma <id> mantenimiento` y
  `alarma <id> finalizar`. Una alarma solo puede operarse desde el grupo de la
  zona de su proyecto; todos los cambios quedan en **Alarm status history**.
- Política anti-spam: cada alarma se anuncia una sola vez al abrirse. Si sigue
  activa sin reconocer no se repite, aunque cambie su severidad. Si la condición
  desaparece sola, se resuelve silenciosamente, conserva su historial y deja de
  aparecer en `resumen`. WhatsApp solo vuelve a responder por una acción humana.
- El grupo general no recibe eventos ni permite cambios. El comando `resumen`
  lista únicamente proyectos con alarmas abiertas, agrupados por su severidad máxima.
- La integración usa la **Groups API oficial**. La cuenta de Meta debe tener esa
  capacidad habilitada; no funciona como un bot agregado a grupos ordinarios existentes.
- **Solo minigranjas (T49)**: `sync_catalog` espeja **únicamente** los proyectos
  con `is_minifarm=True`; los proyectos de autoconsumo ni se guardan, así
  que el admin no los muestra. `dispatch_evaluations` evalúa
  `Project.objects.alarmable()` = `is_minifarm=True` + `monitoring_enabled=True`;
  el dispatcher y `check_sla` filtran igual. Si la API degrada una minigranja, la
  fila se conserva con `is_minifarm=False` (no se pierde su histórico), deja de
  alarmar y queda un WARNING en el log.
- Umbrales COX: defaults en el catálogo (admin → Alarm rules); override por
  proyecto en Rule configs.
- Mantenimientos programados (excluyen alarmas): admin → Maintenance windows.
- Sensores faltantes deliberados: regla puntual por proyecto → admin → Rule
  configs (`enabled=No`); estación meteo completa NO confiable (reporta 0) →
  admin → Projects → `ignore_weather_station` (usa la irradiancia de /power/
  y las reglas de estación no aplican).

## Tests

```bash
.venv/bin/pytest && .venv/bin/ruff check .
```

## Estado conocido (2026-08-29)

- Quoia/frontera migró a `/solarview/measurements/border/historical/`, que exige
  `project_id`, `init_date` y `end_date`. Las reglas 8/9/10 conservan su lógica
  y consultan el día local explícitamente. `/solarview/measurements/border/` se
  usa como oráculo de existencia cuando falla el histórico. El smoke real
  confirmó 63 intervalos en el proyecto probado. Véase
  `docs/API_SOLARVIEW_V1.md`.
- `state` del inversor: strings legibles con formato `"Modo: detalle"`.
  Observados "Grid-connected" y "Standby: insulation resistance detecting"
  (auto-test rutinario, NO falla — la regla 7 exige calificador
  low/fault/abnormal). El vocabulario de DERATING (regla 3) sigue sin
  conocerse: keywords tentativos.
- La API entrega lat/lon invertidas en al menos un proyecto (151, DEPRECATED,
  excluido con `monitoring_enabled=False`): `is_solar_hours` cae al horario
  fijo cuando astral explota por coordenadas inválidas.
- `relay.kw` **nunca se usa en lógica** (T35, decisión de producto): las
  unidades varían por equipo y los relays con firmware desactualizado leen la
  potencia mal (enteros: kw=1 con 34 A fluyendo). El gate de carga de la regla
  18 es por **corriente** (`min_load_current_a`); `pf=0` con corriente se
  reporta como diagnóstico de firmware. "Planta activa" = `active` del relay.
- Autoconsumo: la API **nunca** marca `is_self_consumption` (False en los 77
  proyectos); el único discriminante real es `is_minifarm`. Desde T49 eso
  define el alcance de las alarmas: 37 minigranjas dentro, los otros 40
  proyectos (autoconsumo) fuera. Los guards de `is_self_consumption` en las
  reglas 9/10/18 (T35) siguen ahí, ahora inalcanzables en la práctica.
- Regla 16 (T_mod = `temperature_POA`) activa desde la migración 0004; solo
  la 19 (THD) sigue deshabilitada — la API no expone THD.

## Despliegue en AWS EC2

La producción se ejecuta con [Docker Compose](docker-compose.prod.yml) en una
EC2: Django/Gunicorn, Celery Worker, Celery Beat, PostgreSQL, Redis y Caddy.
Caddy entrega HTTPS; PostgreSQL y Redis permanecen en la red interna de Docker.

### 1. Preparar AWS

- Cree una EC2 Ubuntu Server 24.04 LTS, preferiblemente `t3.medium`, con un
  volumen EBS gp3 de 30–50 GB y una Elastic IP.
- Cree un registro DNS tipo `A` del dominio, por ejemplo
  `alarmas.example.com`, hacia la Elastic IP.
- En el Security Group permita TCP `80` y `443` público. Use Systems Manager o
  limite SSH (`22`) a una IP administrativa. No abra `5432` ni `6379`.
- Permita salida TCP `443` hacia SolarView, Discord y Docker Hub. Con el DNS
  ya propagado, Caddy obtiene y renueva automáticamente el certificado TLS.

### 2. Instalar Docker en la EC2

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git rsync openssl
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Cierre y abra nuevamente la sesión SSH. Compruebe con `docker version` y
`docker compose version`.

### 3. Clonar el repositorio

En la EC2:

```bash
git clone git@github.com:obed-svg/solarview-alarms.git solarview-alarms
cd solarview-alarms
```

Para actualizar el código en una instalación existente:

```bash
git pull --ff-only
./scripts/deploy_ec2.sh --full
```

También puede sincronizar desde la máquina local sin copiar secretos ni
artefactos locales:

```bash
SSH_IDENTITY_FILE=/ruta/instancia.pem ./scripts/upload_ec2.sh \
  ubuntu@IP_O_DNS solarview-alarms --upload-only
```

Cuando `.env.production` ya exista en la EC2, sustituya `--upload-only` por
`--bootstrap` o `--full` para sincronizar y desplegar de una vez.

### 4. Configurar `.env.production`

En el directorio clonado de la EC2 copie el ejemplo de producción; no copie el
`.env` de desarrollo ni suba secretos a Git:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
openssl rand -hex 32  # una salida para SECRET_KEY
openssl rand -hex 32  # otra salida para POSTGRES_PASSWORD
```

Edite `.env.production` y complete, como mínimo:

```env
PUBLIC_HOST=alarmas.example.com
ALLOWED_HOSTS=alarmas.example.com
CSRF_TRUSTED_ORIGINS=https://alarmas.example.com
SECRET_KEY=<valor-largo-y-aleatorio>
POSTGRES_PASSWORD=<valor-largo-y-aleatorio>
SOLARVIEW_BASE_URL=api.sole.tech
static_token=<token-de-solarview>

webhook_discord=<url-del-webhook>
discord_bot_token=<token-del-bot>
discord_application_id=<application-id>
discord_public_key=<public-key>
discord_guild_id=<server-id>
```

Deje `SECURE_HSTS_SECONDS=0` en el primer despliegue. Tras confirmar HTTPS,
puede establecerlo en `31536000`.

### 5. Primer arranque

El modo bootstrap construye la imagen, espera PostgreSQL/Redis, guarda un
backup lógico, aplica migraciones y ejecuta `collectstatic`, pero no inicia
Celery Beat aún:

```bash
./scripts/deploy_ec2.sh --bootstrap
docker compose --env-file .env.production -f docker-compose.prod.yml ps
curl -I "https://$(sed -n 's/^PUBLIC_HOST=//p' .env.production)/health/"

docker compose --env-file .env.production -f docker-compose.prod.yml \
  run --rm web python manage.py createsuperuser
```

### 6. SolarView y Discord

Sincronice primero el catálogo de SolarView:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml \
  run --rm web python manage.py shell -c \
  "from apps.plants.tasks import sync_catalog; print(sync_catalog())"
```

En el portal de desarrolladores de Discord:

1. Cree una aplicación y un bot; copie Application ID, Public Key y Bot Token
   a `.env.production`.
2. Instale la aplicación en el servidor con el scope `applications.commands`.
3. Active *Developer Mode* y copie el ID del servidor a `discord_guild_id`.
4. Cree un canal principal y un webhook; copie su URL a `webhook_discord`.
5. Configure el *Interactions Endpoint URL* como
   `https://DOMINIO/discord/interactions/`.

Recargue los procesos y registre los comandos:

```bash
./scripts/deploy_ec2.sh --bootstrap
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec web python manage.py sync_discord_commands
```

En Django Admin abra **Notification channels > ops-discord** y establezca:

- `Kind: Discord`, `Purpose: Realtime`, `Env key: webhook_discord`.
- El ID del canal principal y la severidad mínima.
- Déjelo deshabilitado hasta que estén asignados los hilos.

Liste los proyectos y asigne un hilo creado dentro del canal principal a cada
proyecto:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec web python manage.py discord_threads

docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec web python manage.py discord_threads --set \
  146=ID_DEL_HILO_146 158=ID_DEL_HILO_158
```

Reemplace los pares de ejemplo por IDs reales. Una alarma sin hilo cae en el
canal principal, pero las acciones interactivas solo se aceptan desde el hilo
del proyecto. Al terminar, habilite `ops-discord`; mantenga deshabilitados los
canales de WhatsApp que no se utilicen.

### 7. Activar alarmas y comprobar operación

```bash
./scripts/deploy_ec2.sh --full
docker compose --env-file .env.production -f docker-compose.prod.yml ps
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec worker celery -A config inspect ping
```

Debe existir un único contenedor `beat`. Revise `Evaluation runs` y
`Notification logs` en Django Admin, y pruebe `/resumen` en Discord. Cada
despliegue crea un backup en `backups/`; programe copias cifradas externas, por
ejemplo a S3, y compruebe restauraciones.

### Validación local antes del commit

```bash
.venv/bin/pytest
.venv/bin/ruff check .

POSTGRES_PASSWORD=validation-only PUBLIC_HOST=localhost \
  DEPLOY_ENV_FILE=.env.production.example \
  docker compose --env-file .env.production.example \
  -f docker-compose.prod.yml config --quiet
bash -n scripts/deploy_ec2.sh scripts/upload_ec2.sh

docker compose -p solarview-alarms-validation \
  --env-file .env.production.example -f docker-compose.prod.yml build
```

Para probar el arranque completo, use puertos locales alternos y elimine el
stack temporal al terminar:

```bash
DEPLOY_ENV_FILE=.env.production.example PUBLIC_HOST=localhost \
  CADDY_HTTP_PORT=18080 CADDY_HTTPS_PORT=18443 \
  docker compose -p solarview-alarms-validation \
  --env-file .env.production.example -f docker-compose.prod.yml up -d --wait postgres redis web worker caddy

curl --fail --insecure https://localhost:18443/health/

docker compose -p solarview-alarms-validation \
  --env-file .env.production.example -f docker-compose.prod.yml \
  down --volumes --remove-orphans
```
