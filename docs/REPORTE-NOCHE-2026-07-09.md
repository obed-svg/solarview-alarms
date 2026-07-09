# Reporte nocturno — 2026-07-08/09

Vigilancia autónoma de la noche completa: runner evaluando los 77 proyectos
cada 5 min (grupo fast; hourly + sync cada hora) contra la API real, con
análisis periódico de si las alarmas tienen sentido. Notificaciones apagadas.
Punto de partida: `f21ee08` (T35).

## Cronología

- **23:07** — Arranque. T35 desplegado en el runner (regla 18 por corriente,
  autoconsumo, derating 80 °C). DB scratch sincronizada: 77 proyectos, 314
  inversores, 0 autoconsumos, 37 minigranjas.
- **23:08** — Primer tick: 35 s / 77 proyectos, 9 alarmas abiertas → análisis.
- **23:16** — T36 commiteado (`0905a8b`), runner reiniciado: las 9 falsas se
  auto-resolvieron en el primer tick; quedan exactamente las 4 legítimas.
- **23:41** — Chequeo: estable. Ticks de 34-42 s / 77 proyectos, `partial=0`,
  sin excepciones. Ruido nocturno esperado en stats (68× `poa:no_verificable`
  en reglas 2/5/6 por tick — semánticamente correcto; posible limpieza
  opcional en la mañana con gates nocturnos si molesta en los EvaluationRun).

## Alarmas legítimas observadas

- **4× `meter_comm_lost` HIGH** (143, 160, 174, 178): medidor con nodos en
  Manager y sin datos, con inversores reportando — el diseño T34 funcionando.
  (149 y 104, los otros dos medidores mudos, no dispararon: sus inversores
  duermen — correcto, no se puede aislar el medidor.)

## Ruido detectado y correcciones aplicadas

1. **8× `weather_comm_lost` falsas (T36)** — las 8 estaciones "murieron" con
   el MISMO `last_data_at` (20:58:10, idéntico al segundo en 6 proyectos): el
   escritor de weather del backend se detiene de noche por sistema. Fix: la
   regla 14 solo evalúa en horario solar (margen 30 min) y de noche ni
   consulta la API. Auto-resueltas al tick siguiente.
2. **1× `project_no_generation` CRITICAL falsa en p118 (T36)** — `/power/`
   entrega irradiance 295-370 W/m² variando como sol de mediodía a las 23:00
   (sin sentido físico para la longitud; primo del lat/lon invertido de T25).
   Fix: cordura física en la regla 1 — fuera del horario solar local no se
   espera generación → `excluded:night`. Auto-resuelta al tick siguiente.

- **00:16-00:25** — Únicas excepciones de la noche: `database is locked` en
  los refrescos de alarmas de medidor durante los ticks hourly (3 en total).
  **Artefacto del runner** (sqlite scratch + 8 hilos escritores;
  `BUSY_SNAPSHOT` en transacciones read→write concurrentes), **no del código
  de producción** (prod = postgres). Fix en dos pasos: WAL + busy_timeout, y
  al persistir, lock de escritura en el runner (las escrituras de alarmas se
  serializan; el fetch de API sigue paralelo). Runner reiniciado 00:26.

- **01:22** — Detectado: p99 y p119 evaluando reglas a la 1 AM. Causa:
  **lat=-1, lon=-1** (centinela "no configurado" de la API) — astral no
  explota, pone el amanecer a la ~01:00 local y corre la ventana solar ~5 h:
  evaluaban de madrugada y quedaban **gateados en su tarde real** (hueco de
  detección diario). Fix T37: amanecer calculado fuera de [04:00, 09:00)
  local = coordenadas no creíbles → horario fijo.
- **01:23** — Segundo hallazgo: había DOS runners vivos en paralelo (un kill
  anterior falló silenciosamente) — explicaba ticks duplicados y un lock más.
  Muerto el viejo por PID; queda solo el del write-lock. Los hourly de 00:23 y
  01:19 con el write-lock: `partial=0`, cero errores.

- **03:27-03:31** — 5 `meter_comm_lost` nuevas al despertar los dataloggers
  de inversores (~03:00): p148/p145/p146 con medidores parados a las
  **20:30:0x exactas** (batch nocturno del backend) y p127/p113 con quoia en
  **cadencia horaria nocturna** (age 61 vs umbral 60 = flap). Fix T38
  (`78dbe7f`): reglas 8 y 14 de noche → `not_computable` (congela abiertas,
  no abre/resuelve en falso; corrige también el flap-al-anochecer que T36
  habría causado a alarmas legítimas de estación). Las 5 quedan abiertas
  para auto-resolverse de día cuando sus medidores retomen — validación
  natural.
- **~03:30** — Runner con `transaction_mode=IMMEDIATE` (Django 5.2) + retry:
  fin definitivo de los locks de sqlite (los `EvaluationRun.create` de otros
  hilos quedaban fuera del write-lock).

- **05:40-05:45 (amanecer)** — 18 alarmas nuevas en 10 min, todas de una
  familia: reglas de ventana evaluando con `now` solar pero ventana de 45 min
  aún nocturna. 13 `poa_invalid` (frozen 0.0 = noche real; offset -1.0 del
  piranómetro; generación difusa cruzando los 5 kW), 1 `data_frozen`
  (temperatura constante nocturna), 3 `recloser_open` **frescos** (60 Hz en
  red, corrientes 0) = plantas que abren el reconectador de noche por
  operación. Fix T39 (`3fdc892`): ventana completamente diurna en 13/15/16
  (margen 60) y margen 30 en la 17; y las 5 reglas con gate nocturno pasan a
  `not_computable` (congelar) — un `ok` nocturno habría resuelto en falso
  cada anochecer las alarmas legítimas del día.

- **06:23-06:43** — Segunda ola matinal: **161 falsas** (73 `inverter_comm_lost`
  con `last_data_at=None` + 88 `availability_inputs_missing`). Los márgenes de
  RELOJ de T23 (amanecer+45) no bastan: los SUN2000 arrancan por IRRADIANCIA —
  a amanecer+45 muchos siguen legítimamente apagados. Fix T40 (`edc2758`):
  reglas 4/12 exigen POA sostenida (>100 W/m², el mismo gate físico de la
  regla 2) antes de exigir comunicación. Cambio semántico consciente en la 12:
  POA faltante ya no dispara por inversor (lo cubren 15/11) — **revisar en la
  mañana**. Las 161 se auto-resuelven con el gate nuevo.

## Para decidir en la mañana

- **Backend**: agregar al reporte la irradiancia nocturna imposible de p118
  (`/project/118/power/` campo `irradiance`) y confirmar que el escritor de
  weather se detiene de noche por diseño (20:58 exacto).
