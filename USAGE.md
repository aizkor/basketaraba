# Guía de uso

Todos los comandos asumen que el entorno virtual está activo:

```bash
source /ruta/al/venv/bin/activate
```

---

## 1. Configuración inicial

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. Pipeline para un solo grupo (paso a paso)

Las tres etapas son independientes y se pueden ejecutar por separado.

### Etapa 1 — Extracción

```bash
# Uso básico: el grupo entre comillas, la salida va a data/<slug>/
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A"

# Con salida detallada
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" -v

# Forzar re-descarga de la caché HTML
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --force

# Directorio de salida personalizado
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --out /tmp/data

# Ajustar la pausa entre peticiones (por defecto: 0,4 s)
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --sleep 1.0

# Guardar métricas en JSON
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --metrics-out metricas.json
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --metrics-history-dir .metricas/
```

### Etapa 2 — Normalización

```bash
# Lee data/<slug>/, escribe data/<slug>/database.json
python stats.py data/senior-masculina-3a-grupo-a

# Ruta de salida personalizada
python stats.py data/senior-masculina-3a-grupo-a --out /tmp/database.json
```

### Etapa 3 — Generación del sitio

```bash
# Un grupo
python web/build.py data/senior-masculina-3a-grupo-a/database.json

# Varios grupos en una sola construcción
python web/build.py data/senior-masculina-3a-grupo-a/database.json \
                    data/senior-femenina-1a-grupo-unico/database.json

# Todos los grupos a la vez (busca data/*/database.json automáticamente)
python web/build.py --all

# Directorios de salida y fuente personalizados
python web/build.py --all --out /tmp/dist --src web/src

# Tema visual (por defecto: compact)
python web/build.py --all --theme dark
```

### Servir en local

```bash
cd web/dist && python -m http.server 8000
# Abre http://localhost:8000
```

---

## 3. Bucle de desarrollo para un grupo

`scripts/run_local_preview.py` encadena las tres etapas y arranca el servidor.

```bash
# Pipeline completo para el grupo predeterminado (SENIOR MASCULINA 3ª-GRUPO A)
python scripts/run_local_preview.py

# Especificar un grupo
python scripts/run_local_preview.py --group "JUNIOR MASCULINA 1A-GRUPO UNICO"

# Saltar etapas ya actualizadas
python scripts/run_local_preview.py --skip-crawl            # solo normalizar y construir
python scripts/run_local_preview.py --skip-crawl --skip-stats  # solo construir y servir

# Forzar re-descarga de la caché
python scripts/run_local_preview.py --force

# Cambiar el puerto
python scripts/run_local_preview.py --port 9000

# Construir sin arrancar el servidor
python scripts/run_local_preview.py --no-serve
```

---

## 4. Pipeline multi-grupo

`scripts/run_all_groups.py` descubre todos los grupos activos en la web, ejecuta el pipeline completo para cada uno y genera un sitio combinado.

```bash
# Descubrir todos los grupos, ejecutar el pipeline completo y servir
python scripts/run_all_groups.py

# Saltar la extracción — usar los datos que ya están en disco
python scripts/run_all_groups.py --skip-crawl

# Solo reconstruir el sitio (sin extracción ni normalización)
python scripts/run_all_groups.py --skip-crawl --skip-stats

# Construir sin arrancar el servidor
python scripts/run_all_groups.py --no-serve
python scripts/run_all_groups.py --skip-crawl --skip-stats --no-serve

# Escanear las últimas 30 semanas al descubrir grupos (más lento, útil fuera de temporada)
python scripts/run_all_groups.py --full-season

# Forzar re-descarga de todas las cachés HTML
python scripts/run_all_groups.py --force

# Cambiar el puerto
python scripts/run_all_groups.py --port 9000
```

---

## 5. Descubrir grupos disponibles

```bash
# Grupos activos en la semana actual y la anterior (rápido, ~2 páginas)
python crawler.py --list-groups

# Escanear las últimas 30 semanas (lento, útil fuera de temporada)
python crawler.py --list-groups --full-season
```

La salida es un array JSON:

```json
[
  {"name": "SENIOR MASCULINA 3A-GRUPO A", "heading": "SEN.MAS.3A-GRUPO A", "category_id": "..."},
  ...
]
```

---

## 6. Re-extraer una temporada finalizada

Cuando la temporada termina, el crawler ya no puede localizar el grupo escaneando las jornadas recientes. Pasa los IDs directamente para saltarte esa búsqueda:

```bash
# --category-id y --group-id se encuentran en group.json o en la URL del sitio
python crawler.py "SENIOR MASCULINA 2A-GRUPO B" \
  --category-id 68403787734a8 \
  --group-id 6888a8711b5b9
```

Para encontrar los IDs de un grupo ya extraído:

```bash
cat data/senior-masculina-2a-grupo-b/group.json
```

---

## 7. Flujos de trabajo habituales

### Actualizar datos de un grupo y previsualizar

```bash
python scripts/run_local_preview.py --group "JUNIOR FEMENINA 1A-GRUPO UNICO"
```

### Reconstruir el sitio sin red (datos ya en disco)

```bash
python scripts/run_local_preview.py --skip-crawl --skip-stats
# o para todos los grupos:
python scripts/run_all_groups.py --skip-crawl --skip-stats
```

### Re-descarga completa (forzar actualización de toda la caché)

```bash
python scripts/run_all_groups.py --force
```

### Construir desde los datos del repositorio (equivalente a CI)

```bash
python web/build.py --all
cd web/dist && python -m http.server 8000
```

### Inspeccionar la base de datos sin el sitio web

```bash
python -c "
import json, pathlib
db = json.loads(pathlib.Path('data/senior-masculina-3a-grupo-a/database.json').read_text())
print([g['name'] for g in db['teams']])
"
```

---

## 8. Referencia de argumentos

### `crawler.py`

| Argumento | Por defecto | Descripción |
|---|---|---|
| `--out` | `data` | Directorio raíz de salida |
| `--sleep` | `0.4` | Segundos entre peticiones |
| `--force` | desactivado | Re-descargar todo el HTML (ignora la caché) |
| `--category-id` | auto | ID de categoría ya resuelto (omite búsqueda en desplegable) |
| `--group-id` | auto | ID de grupo ya resuelto (omite búsqueda en jornadas) |
| `--heading` | auto | Texto de cabecera para filtrar el HTML de jornadas |
| `--list-groups` | — | Imprime un JSON con los grupos disponibles y termina |
| `--full-season` | desactivado | Con `--list-groups`: escanea 30 semanas en vez de 2 |
| `--metrics-out` | — | Escribe métricas en este fichero JSON |
| `--metrics-history-dir` | — | Añade snapshots con timestamp en este directorio |
| `-v` | desactivado | Registro detallado |

### `stats.py`

| Argumento | Por defecto | Descripción |
|---|---|---|
| `--out` | `<dir-grupo>/database.json` | Ruta de salida de la base de datos normalizada |

### `web/build.py`

| Argumento | Por defecto | Descripción |
|---|---|---|
| `--all` | desactivado | Busca todos los `data/*/database.json` automáticamente |
| `--out` | `web/dist` | Directorio de salida del sitio estático |
| `--src` | `web/src` | Directorio fuente con `index.html`, `app.js` y `styles.css` |
| `--theme` | `compact` | Tema visual (`compact`, `white`, `dark`, `editorial`, `glass`…) |

### `scripts/run_local_preview.py`

| Argumento | Por defecto | Descripción |
|---|---|---|
| `--group` | `SENIOR MASCULINA 3ª-GRUPO A` | Grupo a procesar |
| `--force` | desactivado | Actualizar la caché HTML |
| `--skip-crawl` | desactivado | Saltar la etapa 1 |
| `--skip-stats` | desactivado | Saltar la etapa 2 |
| `--skip-build` | desactivado | Saltar la etapa 3 |
| `--no-serve` | desactivado | No arrancar el servidor HTTP |
| `--port` | `8000` | Puerto preferido (se incrementa automáticamente si está ocupado) |

### `scripts/run_all_groups.py`

| Argumento | Por defecto | Descripción |
|---|---|---|
| `--full-season` | desactivado | Escanear 30 semanas al descubrir grupos |
| `--force` | desactivado | Actualizar la caché HTML de todos los grupos |
| `--skip-crawl` | desactivado | Saltar la etapa 1 en todos los grupos |
| `--skip-stats` | desactivado | Saltar la etapa 2 en todos los grupos |
| `--skip-build` | desactivado | Saltar la etapa 3 |
| `--no-serve` | desactivado | No arrancar el servidor HTTP |
| `--port` | `8000` | Puerto preferido (se incrementa automáticamente si está ocupado) |
