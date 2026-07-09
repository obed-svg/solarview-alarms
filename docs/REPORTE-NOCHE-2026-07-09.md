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
- **23:5x** — T36 commiteado, runner reiniciado con los gates nocturnos.

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

## Para decidir en la mañana

- **Backend**: agregar al reporte la irradiancia nocturna imposible de p118
  (`/project/118/power/` campo `irradiance`) y confirmar que el escritor de
  weather se detiene de noche por diseño (20:58 exacto).
