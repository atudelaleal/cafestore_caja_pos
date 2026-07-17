# Caja POS ExpoCafé 2026 — instancia CLOUD (Streamlit Community Cloud)

La Caja del stand corriendo en la nube, **inmune a cortes de luz** del departamento (Pi) o de la oficina (Mac). Lee/escribe **la misma Google Sheet** que la Pi, así que es una copia consistente sin sincronizar nada. **Solo cómputo de venta** — el sync a Odoo se hace aparte, bajo demanda, desde la Pi o el Notebook.

## Qué ya quedó listo en esta carpeta
- `streamlit_app.py` — la Caja adaptada desde la Pi (2026-07-17). Dos cambios vs. la Pi:
  1. El service account se lee de `st.secrets` (no de un archivo).
  2. Los botones de Odoo del panel admin **se ocultan solos** si no hay credenciales de Odoo (que es el caso en la nube). El resumen, las facturas y la reconciliación sí funcionan (leen la planilla).
- `requirements.txt` · `.gitignore` · `.streamlit/secrets.toml.example`
- **Los secretos ya rellenos para pegar** están en un archivo aparte, FUERA de esta carpeta (es sensible):
  `...\scratchpad\secrets_LISTO.toml` — Claude te pasa la ruta exacta en el chat.

---

## PASOS QUE TIENES QUE HACER (≈15–20 min)

### 1) Crear el repo en GitHub (privado)
- github.com → **New repository** → nombre `cafestore-caja-pos` → **Private** → Create.
- Sube estos archivos (por web sirve: botón **Add file → Upload files**, arrastra):
  - `streamlit_app.py`
  - `requirements.txt`
  - `.gitignore`
  - la carpeta `.streamlit/` con **solo** `secrets.toml.example`
- ⚠️ **NO subas** `secrets_LISTO.toml` ni ningún secreto real. (El `.gitignore` ya los bloquea si usas git por consola.)

### 2) Desplegar en Streamlit Community Cloud
- share.streamlit.io → **Sign in with GitHub** → autoriza.
- **Create app → Deploy from GitHub** → repo `cafestore-caja-pos`, branch `main`, main file `streamlit_app.py`.
- Elige un subdominio, ej. `cafestore-caja` → queda `https://cafestore-caja.streamlit.app`.
- **Deploy** (la primera vez tarda ~2–3 min instalando dependencias).

### 3) Cargar los secretos
- En la app desplegada: menú **⋮ → Settings → Secrets**.
- Abre `secrets_LISTO.toml`, **copia TODO** y **pégalo** ahí → **Save**.
- La app se reinicia sola y ya tiene acceso a la Sheet.

### 4) Verificar que funciona
- Venta: `https://<tu-app>.streamlit.app/?token=expocafestore2026ventas&caja=CajaCloud`
  → deberías ver el catálogo (91 productos), agregar al carrito y registrar una venta.
  (Haz una venta de prueba y bórrala de la Sheet después, o márcala como merma.)
- Admin: `https://<tu-app>.streamlit.app/?token=cafestore2026expoadmin`
  → verás resumen/facturas/reconciliación. Los botones de Odoo **no aparecen** (correcto, es la instancia cloud).

### 5) Pinger anti-sueño (gratis, para que nunca duerma)
- uptimerobot.com → crea cuenta → **Add New Monitor** → tipo **HTTP(s)** → URL = tu app → intervalo **5 minutos** → Save.
- Con tráfico durante el evento no duerme igual; el pinger cubre las madrugadas para que el primer acceso de la mañana no tenga cold-start.

### 6) Bookmarks para el evento
- Cada dispositivo abre su URL con su etiqueta: `.../?token=expocafestore2026ventas&caja=Caja1` (`Caja2`, `Barra`, etc.).
- **Cloud = primaria** durante los 3 días. **Pi = fallback** (`https://anteroparietal-marya-lidded.ngrok-free.dev/Caja`).

---

## Notas
- **Mismo Sheet, misma service account** que la Pi → una venta hecha en la cloud aparece en el resumen de la Pi y viceversa. No hay que sincronizar nada.
- **Sync a Odoo:** se sigue haciendo desde la Pi/Notebook (donde sí están las credenciales de Odoo). Esta instancia nunca escribe en Odoo producción.
- **Actualizar el código:** si algún día cambias `streamlit_app.py`, súbelo al repo y Streamlit Cloud redesplega solo. (Si quieres mantenerlo igual a la Pi, reaplica los 2 cambios de arriba sobre la versión nueva de la Pi.)
- **Lo que la nube NO cubre:** si se cae el internet **del recinto**, los dispositivos no llegan a ningún servidor. Fallback offline: la Sheet abierta en un celular con datos, o papel.
