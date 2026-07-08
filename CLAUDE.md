# CLAUDE.md

## Seguridad: .env

`.env` contiene secretos y no debe ser leído por Claude. Hay un hook en
`.claude/settings.local.json` (PreToolUse) que bloquea `Read`, `Edit`,
`Write`, `NotebookEdit` y `Bash` sobre `.env`/`.env.*`.

**Límite conocido:** ese hook solo intercepta llamadas a tools. Mencionar el
archivo con `@.env` en el prompt inyecta su contenido directo al contexto,
sin pasar por ninguna tool ni hook — el bloqueo no aplica ahí. No usar
`@.env` (ni `@.env.*`) en prompts, ni en loops o automatizaciones (`/loop`,
cron, etc.) que puedan referenciarlo por mención directa.
