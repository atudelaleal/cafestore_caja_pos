# streamlit_app.py — Caja POS ExpoCafe 2026 (instancia CLOUD)
# Adaptado de pages/10_Caja.py de la Home Pi (2026-07-17). Cambios vs. la Pi:
#   1) el service account se lee de st.secrets['gcp_service_account'] (con fallback a archivo)
#   2) los botones de Odoo del panel admin se ocultan si no hay credenciales de Odoo (odoo_enabled())
# El flujo de venta es identico: lee/escribe la MISMA Google Sheet que la Pi.

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
from datetime import datetime
import pytz
import xmlrpc.client

st.set_page_config(
    page_title="Caja ExpoCafé", page_icon="💰", layout="wide",
    initial_sidebar_state="collapsed",
)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
SA_PATH = ".streamlit/gcp_service_account.json"
CHILE_TZ = pytz.timezone('America/Santiago')
METODOS_PAGO = ["Efectivo", "Transbank", "Mercado Pago", "Transferencia"]
COLS_GRID = 6

# ---- Tokens de acceso (vista) ----
TOKEN_ADMIN = "cafestore2026expoadmin"
TOKEN_VENTAS = "expocafestore2026ventas"

# ---- IDs fijos de Odoo (ver nota "Caja POS - Plan B", Referencia rapida) ----
ODOO_COMPANY_ID = 1          # Sociedad Comercial Ubuntu Limitada
ODOO_WAREHOUSE_ID = 10       # Eventos Temporales Expo (EVENT) CS id=1, Event id =10
ODOO_LOCATION_EVENT = 163    # EVENT/Stock
ODOO_CARRIER_ID = 39         # Pago presencial - retirado en local (gatilla cron id=110)
ODOO_PRICELIST_ID = 4963     # ExpoCafe (CLP)
ODOO_USER_ID = 36            # Eventos / Proyectos (anibal@cafestore.cl)
ODOO_PARTNER_ID = 127749     # Cliente Expo
ODOO_GENERIC_PRODUCT_ID = 15506  # PROD-GEN-VEN (fallback si el SKU no matchea)
ODOO_TAX_FALLBACK = 1        # IVA 19% Venta Incluye Impuestos (Ubuntu)


def _qp_get(name, default=""):
    """Lee un query param de la URL de forma robusta a la versión de Streamlit."""
    try:
        v = st.query_params.get(name, default)                       # Streamlit >= 1.30
    except Exception:
        try:
            v = st.experimental_get_query_params().get(name, default)  # Streamlit < 1.30
        except Exception:
            v = default
    if isinstance(v, list):
        v = v[0] if v else default
    return v


@st.cache_resource
def get_gspread_client():
    """Cliente de gspread con reintentos automáticos ante fallas de conexión transitorias
    (ej. conexión keep-alive reciclada que el servidor ya cerró — se veía como
    ConnectionError/RemoteDisconnected intermitente tras un rato sin uso). El cliente vive
    cacheado por el proceso del contenedor; sin esto, la primera llamada tras un hueco de
    inactividad podía fallar de forma visible para quien está vendiendo."""
    if "gcp_service_account" in st.secrets:
        creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
    session = AuthorizedSession(creds)
    retry_policy = Retry(
        total=5,
        backoff_factor=0.7,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=None,  # incluye POST — necesario para reintentar "enviar venta"
    )
    adapter = HTTPAdapter(max_retries=retry_policy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return gspread.authorize(creds, session=session)


def get_sheet():
    if "pos_backup_spreadsheet_id" not in st.secrets:
        st.error("Falta configurar `pos_backup_spreadsheet_id` en secrets.toml")
        st.stop()
    return get_gspread_client().open_by_key(st.secrets["pos_backup_spreadsheet_id"])


def fetch_data():
    """Trae Stock y Ventas frescos desde Google Sheets. Solo se llama cuando se pide explícitamente
    (carga inicial, botón Actualizar, o al enviar/validar una venta) — nunca por un timer."""
    sh = get_sheet()
    stock_ws = sh.worksheet("Stock")
    ventas_ws = sh.worksheet("Ventas")
    stock_df = pd.DataFrame(stock_ws.get_all_records())
    ventas_df = pd.DataFrame(ventas_ws.get_all_records())

    stock_df["SKU"] = stock_df["SKU"].astype(str)
    stock_df["Stock Inicial"] = pd.to_numeric(stock_df["Stock Inicial"], errors="coerce").fillna(0)
    stock_df["Reposición"] = pd.to_numeric(stock_df.get("Reposición", 0), errors="coerce").fillna(0)
    # La hoja ahora trae "Precio Expo" (calculado desde Precio Venta Odoo x Descuento, ver Fase 1
    # de sincronización con Odoo) — se mantiene como stock_df["Precio"] internamente para no tocar
    # el resto del código (carrito, catálogo, itertuples).
    stock_df["Precio"] = pd.to_numeric(stock_df["Precio Expo"], errors="coerce").fillna(0)
    stock_df["Score"] = pd.to_numeric(stock_df.get("Score", 0), errors="coerce").fillna(0)

    if not ventas_df.empty:
        ventas_df["SKU"] = ventas_df["SKU"].astype(str)
        ventas_df["Cantidad"] = pd.to_numeric(ventas_df["Cantidad"], errors="coerce").fillna(0)
        ventas_df["Subtotal"] = pd.to_numeric(ventas_df["Subtotal"], errors="coerce").fillna(0)
        if "Merma" in ventas_df.columns:
            ventas_df["Merma"] = ventas_df["Merma"].astype(str).str.upper().isin(["TRUE", "SÍ", "SI", "1"])
        else:
            ventas_df["Merma"] = False
        if "Factura" in ventas_df.columns:
            ventas_df["Factura"] = ventas_df["Factura"].astype(str).str.upper().isin(["TRUE", "SÍ", "SI", "1", "VERDADERO"])
        else:
            ventas_df["Factura"] = False
        # Ojo: `vendido` suma ventas + mermas + facturas — todas son salidas físicas reales del stand.
        vendido = ventas_df.groupby("SKU")["Cantidad"].sum()
    else:
        vendido = pd.Series(dtype=float)

    stock_df["Vendido"] = stock_df["SKU"].map(vendido).fillna(0)
    stock_df["Stock restante"] = stock_df["Stock Inicial"] + stock_df["Reposición"] - stock_df["Vendido"]
    # Orden del catálogo: primero lo más vendido EN ESTE EVENTO; empate se rompe con el score
    # histórico de popularidad (ExpoCafé2025 + CyberDay + 12m Odoo) para que el día 1, con 0 ventas
    # propias todavía, ya aparezcan primero los productos que sabemos que rotan más.
    stock_df = stock_df.sort_values(["Vendido", "Score"], ascending=[False, False]).reset_index(drop=True)
    return stock_df, ventas_df, ventas_ws


def refrescar():
    st.session_state.stock_df, st.session_state.ventas_df, st.session_state.ventas_ws = fetch_data()


def money(n):
    return f"${int(n):,}".replace(",", ".")


def append_ventas(ventas_ws, rows):
    """Agrega filas a la hoja Ventas alineadas al encabezado vigente: cada fila es un dict y se
    coloca cada valor bajo su columna, dejando en blanco las que no vengan (ej. Estado_Sync).
    Crea las columnas que la vista de venta escribe si aún no existen (expandiendo la grilla si hace
    falta, ver ensure_columns). Robusto ante columnas agregadas por el admin."""
    header = ensure_columns(ventas_ws, ["Dispositivo", "Factura", "Email_Factura", "Descuento"])
    matrix = [[r.get(col, "") for col in header] for r in rows]
    ventas_ws.append_rows(matrix, value_input_option="USER_ENTERED")


def arg_sep(sh):
    """Separador de argumentos de fórmula según el locale del Sheet (',' en inglés, ';' en
    locales con coma decimal como es_CL). Evita que SUMIF falle por el separador equivocado."""
    try:
        loc = sh.fetch_sheet_metadata().get("properties", {}).get("locale", "en_US")
    except Exception:
        loc = "en_US"
    return "," if str(loc).startswith("en") else ";"


# ============================================================================
#  ODOO — helpers de solo lectura y de escritura (usados solo por el panel admin)
# ============================================================================

def odoo_enabled():
    """En la instancia cloud NO cargamos credenciales de Odoo (solo computo de venta).
    El sync a Odoo se hace bajo demanda desde una maquina de confianza (Pi/Notebook)."""
    return "url_cs" in st.secrets


@st.cache_resource
def get_odoo():
    """Conexión XML-RPC a Odoo producción. Reusa las credenciales del proyecto de reportes
    (url_cs/db_cs/username_cs/password_cs en secrets.toml)."""
    url = st.secrets["url_cs"]
    db = st.secrets["db_cs"]
    user = st.secrets["username_cs"]
    key = st.secrets["password_cs"]
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, key, {})
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return uid, key, db, models


def odoo_sr(model, domain, fields):
    uid, key, db, models = get_odoo()
    return models.execute_kw(db, uid, key, model, "search_read", [domain], {"fields": fields})


def odoo_lookup_products(skus):
    """Busca en Odoo los product.product cuyo default_code está en `skus`.
    Devuelve (by_sku, duplicados, stock_en_EVENT). Solo lectura."""
    skus = [s for s in {str(s) for s in skus} if s]
    prods = odoo_sr("product.product", [["default_code", "in", skus]],
                    ["id", "default_code", "name", "lst_price", "taxes_id", "uom_id"])
    by_sku, dup = {}, set()
    for p in prods:
        code = p["default_code"]
        if code in by_sku:
            dup.add(code)
        by_sku[code] = p
    pids = [p["id"] for p in prods]
    stock_by_pid = {}
    if pids:
        quants = odoo_sr("stock.quant",
                         [["product_id", "in", pids], ["location_id", "=", ODOO_LOCATION_EVENT]],
                         ["product_id", "quantity"])
        for q in quants:
            pid = q["product_id"][0]
            stock_by_pid[pid] = stock_by_pid.get(pid, 0) + q["quantity"]
    return by_sku, dup, stock_by_pid


def get_generic_product():
    g = odoo_sr("product.product", [["id", "=", ODOO_GENERIC_PRODUCT_ID]], ["id", "name", "taxes_id", "uom_id"])
    if g:
        return g[0]
    return {"id": ODOO_GENERIC_PRODUCT_ID, "name": "Producto Generico Ventas",
            "taxes_id": [ODOO_TAX_FALLBACK], "uom_id": [1, "Unidades"]}


def resolve_tax_ids(product, tax_cache):
    """Devuelve los impuestos de venta del producto que pertenecen a la compañía Ubuntu (id=1),
    o sin compañía. Evita el bug multi-compañía (un IVA idéntico de Mundo Novo, id=26)."""
    pid = product["id"]
    if pid in tax_cache:
        return tax_cache[pid]
    tids = product.get("taxes_id") or []
    result = [ODOO_TAX_FALLBACK]
    if tids:
        taxes = odoo_sr("account.tax", [["id", "in", tids]], ["id", "company_id"])
        ok = [t["id"] for t in taxes if (not t["company_id"]) or t["company_id"][0] == ODOO_COMPANY_ID]
        if ok:
            result = ok
    tax_cache[pid] = result
    return result


def ensure_columns(ws, names):
    """Garantiza que la hoja tenga las columnas `names` en el encabezado (fila 1). Las agrega al final
    si faltan, **expandiendo la grilla física si no hay espacio**. Devuelve el encabezado actualizado.

    Lo de expandir importa: Sheets rechaza escribir fuera de la grilla aunque la celda "se vea" vacía
    (APIError 400 'exceeds grid limits'). Pasó de verdad el 2026-07-14 con la columna `Dispositivo`
    y hubo que agregar columnas a mano — esto lo evita para cualquier columna futura."""
    header = ws.row_values(1)
    faltantes = [n for n in names if n not in header]
    if not faltantes:
        return header
    necesarias = len(header) + len(faltantes)
    if necesarias > ws.col_count:
        ws.add_cols(necesarias - ws.col_count)
    for n in faltantes:
        header.append(n)
        ws.update_cell(1, len(header), n)
    return header


def _is_merma(r):
    return str(r.get("Merma", "")).strip().upper() in ("TRUE", "SÍ", "SI", "1", "VERDADERO")


def _is_factura(r):
    """Venta marcada como 'requiere factura'. Se salta el envío a Odoo: la factura necesita el
    cliente real (RUT), no el genérico Cliente Expo — Álvaro contacta al correo capturado, pide
    los datos y crea el pedido + factura a mano. Mismo criterio que las mermas."""
    return str(r.get("Factura", "")).strip().upper() in ("TRUE", "SÍ", "SI", "1", "VERDADERO")


def accion_consultar_odoo():
    """BOTÓN 3 (solo lectura). Trae lst_price + stock en EVENT/Stock desde Odoo hacia la hoja Stock,
    marca cada SKU como OK / NO ENCONTRADO / DUPLICADO, y (re)escribe la fórmula de 'Stock actual'
    (= Stock Inicial − ventas y mermas de la Caja) en cada fila. No escribe nada en Odoo."""
    from gspread.utils import rowcol_to_a1

    def col_letter(idx):
        return rowcol_to_a1(1, idx).rstrip("0123456789")

    sh = get_sheet()
    ws = sh.worksheet("Stock")
    sep = arg_sep(sh)
    records = ws.get_all_records()
    header = ensure_columns(ws, ["Estado_Match", "Nombre Odoo", "Reposición", "Stock actual"])
    idx = {name: header.index(name) + 1
           for name in ["Precio Venta Odoo", "Stock Odoo", "Estado_Match", "Nombre Odoo", "Stock actual"]}
    L_sku = col_letter(header.index("SKU") + 1)
    L_ini = col_letter(header.index("Stock Inicial") + 1)
    L_repo = col_letter(header.index("Reposición") + 1)

    skus = [str(r.get("SKU", "")) for r in records]
    by_sku, dup, stock_by_pid = odoo_lookup_products(skus)

    updates, report = [], []
    for i, r in enumerate(records):
        sku = str(r.get("SKU", ""))
        if not sku:
            continue
        row = i + 2
        p = by_sku.get(sku)
        if not p:
            estado, precio, stock, nombre = "NO ENCONTRADO", "", "", ""
        else:
            estado = "DUPLICADO" if sku in dup else "OK"
            precio = p["lst_price"]
            stock = stock_by_pid.get(p["id"], 0)
            nombre = p["name"]
        if p:
            updates.append({"range": rowcol_to_a1(row, idx["Precio Venta Odoo"]), "values": [[precio]]})
            updates.append({"range": rowcol_to_a1(row, idx["Stock Odoo"]), "values": [[stock]]})
        updates.append({"range": rowcol_to_a1(row, idx["Estado_Match"]), "values": [[estado]]})
        updates.append({"range": rowcol_to_a1(row, idx["Nombre Odoo"]), "values": [[nombre]]})
        # Stock actual = Stock Inicial + Reposición − (ventas + mermas de la hoja Ventas para ese SKU)
        formula = f"={L_ini}{row}+{L_repo}{row}-SUMIF(Ventas!$B:$B{sep}${L_sku}{row}{sep}Ventas!$D:$D)"
        updates.append({"range": rowcol_to_a1(row, idx["Stock actual"]), "values": [[formula]]})
        report.append({"SKU": sku, "Producto (hoja)": r.get("Producto", ""), "Nombre en Odoo": nombre,
                       "Precio Odoo": precio, "Stock Odoo": stock, "Estado": estado})

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return pd.DataFrame(report)


def accion_enviar_ventas(simular=True):
    """BOTÓN 2 (escribe en Odoo cuando simular=False). Crea un sale.order por cada Venta_ID no
    sincronizado. SKU sin match -> producto genérico con el nombre en la descripción de la línea.
    Salta mermas, ventas con factura y ventas ya sincronizadas. Marca ERROR sin reintentar solas."""
    from gspread.utils import rowcol_to_a1
    ws = get_sheet().worksheet("Ventas")
    records = ws.get_all_records()
    header = ensure_columns(ws, ["Estado_Sync", "Pedido_Odoo", "Factura", "Email_Factura"])
    col_estado = header.index("Estado_Sync") + 1
    col_pedido = header.index("Pedido_Odoo") + 1

    grupos = {}
    for i, r in enumerate(records):
        vid = str(r.get("Venta_ID", ""))
        grupos.setdefault(vid, []).append((i + 2, r))

    by_sku, dup, _ = odoo_lookup_products([str(r.get("SKU", "")) for r in records])
    generic = get_generic_product()
    tax_cache = {}

    # Ventas con factura: SÍ se cargan a Odoo, pero quedan en BORRADOR (sin action_confirm) y con
    # una nota en el chatter con el correo del cliente. Así el pedido no se pierde y Álvaro solo
    # cambia el cliente y confirma, en vez de tipearlo de nuevo.
    # ⚠️ Un borrador NO toca el stock (no reserva ni descuenta) — lo confirmó el test S08308: en
    # draft no pasó nada, recién al confirmar apareció la entrega EVENT/OUT/00009. Por eso estas
    # ventas siguen contando en la tabla de reconciliación como "no descontado en Odoo".
    plan = []
    for vid, rows in grupos.items():
        if not vid:
            continue
        if any(str(rr.get("Estado_Sync", "")).upper() in ("OK", "FACTURA") for _, rr in rows):
            continue  # ya sincronizada (OK) o ya cargada como borrador de factura
        es_factura = any(_is_factura(rr) for _, rr in rows)
        email_factura = next((str(rr.get("Email_Factura", "")).strip() for _, rr in rows
                              if str(rr.get("Email_Factura", "")).strip()), "")
        lines_src = [(rn, rr) for rn, rr in rows if not _is_merma(rr)]
        if not lines_src:
            continue  # todo merma -> fuera de alcance
        order_lines, detalle = [], []
        for rn, rr in lines_src:
            sku = str(rr.get("SKU", ""))
            qty = float(rr.get("Cantidad", 0) or 0)
            subtotal = float(rr.get("Subtotal", 0) or 0)
            if qty <= 0:
                continue
            price_unit = subtotal / qty  # IVA incluido (así lo maneja la Caja)
            p = None if sku in dup else by_sku.get(sku)
            if p:
                pid, name = p["id"], p["name"]
                uom = (p.get("uom_id") or [1])[0]
                taxes = resolve_tax_ids(p, tax_cache)
                match = "OK"
            else:
                pid = generic["id"]
                name = f'{rr.get("Producto", "")} [{sku}]'.strip()
                uom = (generic.get("uom_id") or [1])[0]
                taxes = resolve_tax_ids(generic, tax_cache)
                match = "GENERICO(DUP)" if sku in dup else "GENERICO"
            order_lines.append([0, 0, {
                "product_id": pid,
                "name": name,
                "product_uom_qty": qty,
                "product_uom": uom,
                "price_unit": price_unit,
                "tax_id": [[6, 0, taxes]],
            }])
            detalle.append({"Venta_ID": vid, "SKU": sku, "Cant": qty, "P.Unit": round(price_unit),
                            "Se registra como": name, "Match": match})
        if order_lines:
            plan.append({"vid": vid, "rows": [rn for rn, _ in lines_src],
                         "order_lines": order_lines, "detalle": detalle,
                         "es_factura": es_factura, "email": email_factura})

    detalle_all = [d for pl in plan for d in pl["detalle"]]
    n_factura = sum(1 for pl in plan if pl["es_factura"])

    if simular:
        # La simulación no escribe NADA (ni en Odoo ni en la hoja) — solo informa.
        return {"simulado": True, "n_ventas": len(plan), "n_factura": n_factura,
                "detalle": pd.DataFrame(detalle_all), "resultados": pd.DataFrame()}

    uid, key, db, models = get_odoo()
    ctx = {"allowed_company_ids": [ODOO_COMPANY_ID]}
    updates, resultados = [], []
    for pl in plan:
        try:
            order_id = models.execute_kw(db, uid, key, "sale.order", "create", [{
                "company_id": ODOO_COMPANY_ID,
                "partner_id": ODOO_PARTNER_ID,
                "user_id": ODOO_USER_ID,
                "warehouse_id": ODOO_WAREHOUSE_ID,
                "pricelist_id": ODOO_PRICELIST_ID,
                "carrier_id": ODOO_CARRIER_ID,
                "order_line": pl["order_lines"],
            }], {"context": ctx})
            name = models.execute_kw(db, uid, key, "sale.order", "read", [[order_id], ["name"]])[0]["name"]

            if pl["es_factura"]:
                # NO se confirma: la factura va al cliente real, no a Cliente Expo. Queda en borrador
                # para que Álvaro cambie el cliente y recién ahí confirme. El cron id=110 no lo toca
                # (valida entregas, y un borrador no genera entrega).
                nota = ("🧾 <b>El cliente pidió FACTURA</b><br/>"
                        f"Contactar a: <b>{pl['email'] or '(sin correo registrado)'}</b> "
                        "para pedir RUT y razón social.<br/>"
                        "Pasos: cambiar el cliente (hoy es <i>Cliente Expo</i>, genérico) → "
                        "verificar que el precio no cambie al reasignar (la lista debe seguir siendo "
                        "<i>ExpoCafe</i>) → confirmar el pedido → emitir la factura.<br/>"
                        f"Origen: Caja ExpoCafé (Plan B), Venta_ID <code>{pl['vid']}</code>.")
                try:
                    models.execute_kw(db, uid, key, "sale.order", "message_post", [[order_id]],
                                      {"body": nota})
                except Exception:
                    pass  # el chatter es informativo — que falle no debe voltear el pedido creado
                for rn in pl["rows"]:
                    updates.append({"range": rowcol_to_a1(rn, col_estado), "values": [["FACTURA"]]})
                    updates.append({"range": rowcol_to_a1(rn, col_pedido), "values": [[name]]})
                resultados.append({"Venta_ID": pl["vid"], "Estado": "FACTURA (borrador)",
                                   "Pedido/Detalle": f"{name} — sin confirmar, contactar {pl['email']}"})
            else:
                models.execute_kw(db, uid, key, "sale.order", "action_confirm", [[order_id]], {"context": ctx})
                for rn in pl["rows"]:
                    updates.append({"range": rowcol_to_a1(rn, col_estado), "values": [["OK"]]})
                    updates.append({"range": rowcol_to_a1(rn, col_pedido), "values": [[name]]})
                resultados.append({"Venta_ID": pl["vid"], "Estado": "OK", "Pedido/Detalle": name})
        except Exception as e:
            for rn in pl["rows"]:
                updates.append({"range": rowcol_to_a1(rn, col_estado), "values": [["ERROR"]]})
            resultados.append({"Venta_ID": pl["vid"], "Estado": "ERROR", "Pedido/Detalle": str(e)[:250]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return {"simulado": False, "n_ventas": len(plan), "n_factura": n_factura,
            "detalle": pd.DataFrame(detalle_all), "resultados": pd.DataFrame(resultados)}


# ============================================================================
#  PANEL ADMIN  (?token=cafestore2026expoadmin)
# ============================================================================

def render_admin():
    # Compacto a propósito (misma lógica que la vista de vendedor) — admin y vendedor nunca
    # corren en la misma carga de página (esta rama termina en st.stop() en el ruteo por token),
    # así que este CSS nunca afecta la vista de vendedor.
    st.markdown("""
        <style>
        .block-container{padding-top:4.5rem;}
        [data-testid="stVerticalBlock"]{gap:0.6rem;}
        [data-testid="stMetricValue"]{font-size:1.6rem;}
        </style>
    """, unsafe_allow_html=True)
    st.subheader("🛠️ Panel Admin — Caja ExpoCafé 2026")
    st.caption("Acciones manuales, ejecutables por botón (sin cron durante las pruebas). "
               "Solo visible con el token admin.")

    # ---- Resumen de ventas ----
    try:
        stock_df, ventas_df, _ = fetch_data()
    except Exception as e:
        stock_df, ventas_df = pd.DataFrame(), pd.DataFrame()
        st.warning(f"No se pudo leer ventas: {e}")

    st.subheader("📊 Resumen de ventas")
    if ventas_df.empty:
        st.info("Sin ventas registradas todavía.")
    else:
        reales = ventas_df[~ventas_df["Merma"]] if "Merma" in ventas_df.columns else ventas_df
        merma = ventas_df[ventas_df["Merma"]] if "Merma" in ventas_df.columns else ventas_df.iloc[0:0]
        cols = st.columns(4)
        cols[0].metric("TOTAL ventas reales", money(reales["Subtotal"].sum()))
        cols[1].metric("N° transacciones", int(reales["Venta_ID"].nunique()) if "Venta_ID" in reales else len(reales))
        cols[2].metric("N° líneas", len(reales))
        cols[3].metric("📉 Merma (ref.)", money(merma["Subtotal"].sum()) if not merma.empty else "—")
        por_metodo = reales.groupby("Metodo_Pago")["Subtotal"].sum().sort_values(ascending=False)
        mc = st.columns(max(len(por_metodo), 1))
        for i, (metodo, tot) in enumerate(por_metodo.items()):
            mc[i].metric(metodo, money(tot))
        # Pendientes: las de factura se cuentan aparte — sí van a Odoo, pero como BORRADOR sin
        # confirmar (Estado_Sync=FACTURA), así que no son "pendientes de enviar" ni están cerradas.
        facturas = reales[reales["Factura"]] if "Factura" in reales.columns else reales.iloc[0:0]
        if "Venta_ID" in reales.columns:
            if "Estado_Sync" in ventas_df.columns:
                sync_ok = ventas_df[ventas_df["Estado_Sync"].astype(str).str.upper() == "OK"]["Venta_ID"].astype(str).unique()
            else:
                sync_ok = []
            vids_factura = set(facturas["Venta_ID"].astype(str).unique()) if not facturas.empty else set()
            sin_factura = reales[~reales["Factura"]] if "Factura" in reales.columns else reales
            pend = [v for v in sin_factura["Venta_ID"].astype(str).unique()
                    if v not in set(sync_ok) and v not in vids_factura]
            st.caption(f"📤 Ventas pendientes de enviar a Odoo: **{len(pend)}** "
                       f"· 🧾 con factura (borrador en Odoo, falta emitir): **{len(vids_factura)}**")

        # ---- Facturas pendientes de emitir ----
        if not facturas.empty:
            st.markdown("**🧾 Ventas con factura — pendientes de emitir en Odoo:**")
            st.caption("Se cargan a Odoo como **borrador sin confirmar** (columna `Pedido_Odoo` = el "
                       "presupuesto creado), con una nota en el chatter. Para cerrarlas: contacta al "
                       "correo → pide RUT/razón social → cambia el cliente (hoy es *Cliente Expo*) → "
                       "**verifica que el precio no cambie al reasignar** → confirma → emite la factura.")
            cols_f = [c for c in ["Timestamp", "Venta_ID", "Producto", "Cantidad", "Subtotal",
                                  "Email_Factura", "Metodo_Pago", "Dispositivo"] if c in facturas.columns]
            st.dataframe(facturas[cols_f], use_container_width=True, hide_index=True)
            st.caption(f"Total facturado pendiente: **{money(facturas['Subtotal'].sum())}** "
                       f"en **{facturas['Venta_ID'].nunique()}** transacción(es)")

        # ---- Reconciliación: qué salió físicamente pero Odoo todavía no sabe ----
        # Odoo solo descuenta las ventas sincronizadas. Todo lo demás (mermas, facturas, ventas aún
        # sin enviar) sale del stand pero no de Odoo -> Stock actual = Stock Odoo − esta brecha.
        if not ventas_df.empty and "SKU" in ventas_df.columns:
            sync_ok_set = set(sync_ok) if "Venta_ID" in reales.columns else set()
            no_en_odoo = ventas_df.copy()
            no_en_odoo["_sincronizada"] = no_en_odoo["Venta_ID"].astype(str).isin(sync_ok_set)
            no_en_odoo = no_en_odoo[~no_en_odoo["_sincronizada"]]
            if not no_en_odoo.empty:
                def _motivo(r):
                    if r["Merma"]:
                        return "Merma"
                    if r.get("Factura", False):
                        return "Factura"
                    return "Pendiente sync"
                no_en_odoo["Motivo"] = no_en_odoo.apply(_motivo, axis=1)
                tabla = (no_en_odoo.pivot_table(index="SKU", columns="Motivo", values="Cantidad",
                                                aggfunc="sum", fill_value=0)
                         .reset_index())
                nombres = stock_df[["SKU", "Producto"]] if not stock_df.empty else pd.DataFrame(columns=["SKU", "Producto"])
                tabla = tabla.merge(nombres, on="SKU", how="left")
                motivo_cols = [c for c in ["Merma", "Factura", "Pendiente sync"] if c in tabla.columns]
                tabla["TOTAL no descontado en Odoo"] = tabla[motivo_cols].sum(axis=1)
                orden = ["SKU", "Producto"] + motivo_cols + ["TOTAL no descontado en Odoo"]
                with st.expander(f"⚖️ Reconciliación de stock — {len(tabla)} SKU con unidades que Odoo aún no descuenta"):
                    st.caption("**Stock actual (físico) = Stock Odoo − TOTAL no descontado en Odoo.** "
                               "Las 'Pendiente sync' desaparecen al presionar el botón 2. "
                               "Las de **Factura** siguen contando aunque ya tengan pedido en Odoo: un "
                               "**borrador no mueve stock**, recién descuenta al confirmarlo. "
                               "Las de **Merma** se van cuando las registras a mano.")
                    st.dataframe(tabla[orden], use_container_width=True, hide_index=True)

        if "Dispositivo" in ventas_df.columns:
            st.markdown("**Por dispositivo (quién vendió / quién registró mermas):**")
            dv = reales.groupby("Dispositivo")["Subtotal"].agg(Ventas_CLP="sum", Lineas_venta="count")
            if not merma.empty and "Dispositivo" in merma.columns:
                dm = merma.groupby("Dispositivo")["Subtotal"].agg(Merma_CLP="sum", Lineas_merma="count")
                dv = dv.join(dm, how="outer")
            st.dataframe(dv.fillna(0), use_container_width=True)

    st.divider()

    if not odoo_enabled():
        st.info("🔒 **Sync con Odoo deshabilitado en esta instancia** (solo computo de venta). "
                "Las ventas quedan guardadas en la planilla; la validacion y el envio a Odoo se hacen "
                "bajo demanda desde la Pi o el Notebook. El resumen, las facturas y la reconciliacion "
                "de arriba si funcionan (leen la planilla).")
        return

    # ---- 1) Consultar Odoo (solo lectura) ----
    st.subheader("1) 🔎 Consultar Odoo — precio, stock y validación de SKU (solo lectura)")
    st.caption("Trae `lst_price` y el stock en EVENT/Stock desde Odoo hacia la hoja **Stock**, y marca "
               "cada SKU como **OK / NO ENCONTRADO / DUPLICADO** (columnas `Estado_Match` y `Nombre Odoo`). "
               "Úsalo tras agregar SKUs nuevos (ej. café) para confirmar que vinculan bien. No escribe en Odoo.")
    if st.button("🔎 Consultar Odoo ahora", use_container_width=True, type="primary"):
        with st.spinner("Consultando Odoo (solo lectura)..."):
            try:
                st.session_state.admin_consulta = accion_consultar_odoo()
                st.session_state.admin_consulta_err = None
            except Exception as e:
                st.session_state.admin_consulta_err = str(e)
    if st.session_state.get("admin_consulta_err"):
        st.error(f"Error al consultar Odoo: {st.session_state.admin_consulta_err}")
    if isinstance(st.session_state.get("admin_consulta"), pd.DataFrame):
        df = st.session_state.admin_consulta
        n_ok = int((df["Estado"] == "OK").sum())
        n_no = int((df["Estado"] == "NO ENCONTRADO").sum())
        n_dup = int((df["Estado"] == "DUPLICADO").sum())
        st.success(f"✅ OK: {n_ok}  ·  ❌ NO ENCONTRADO: {n_no}  ·  ⚠️ DUPLICADO: {n_dup}")
        no_ok = df[df["Estado"] != "OK"]
        if not no_ok.empty:
            st.markdown("**SKUs que NO vincularon bien (revisar):**")
            st.dataframe(no_ok, use_container_width=True, hide_index=True)
        with st.expander(f"Ver los {len(df)} SKUs consultados"):
            st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()

    # ---- 2) Enviar ventas a Odoo (escribe) ----
    st.subheader("2) 📤 Enviar ventas a Odoo (crea pedidos)")
    st.caption("Crea un pedido por cada `Venta_ID` no sincronizado. SKU sin match → **Producto Genérico** "
               "con el nombre del ítem en la descripción de la línea (para tracking). Salta mermas y ventas "
               "ya enviadas. **Empieza siempre en modo simulación.**")
    escribir = st.checkbox("⚠️ Desactivar simulación y ESCRIBIR pedidos reales en Odoo producción",
                           value=False, key="admin_write")
    confirmar = False
    if escribir:
        confirmar = st.checkbox("Confirmo crear pedidos reales en Odoo (Ubuntu Limitada)", key="admin_confirm")
    label = "📤 Ejecutar envío REAL a Odoo" if escribir else "🧪 Simular envío (no escribe)"
    if st.button(label, use_container_width=True, type=("primary" if escribir else "secondary")):
        if escribir and not confirmar:
            st.warning("Marca la casilla de confirmación para escribir en Odoo, o deja la simulación activa.")
        else:
            with st.spinner("Procesando ventas..."):
                try:
                    st.session_state.admin_envio = accion_enviar_ventas(simular=not escribir)
                    st.session_state.admin_envio_err = None
                except Exception as e:
                    st.session_state.admin_envio_err = str(e)
    if st.session_state.get("admin_envio_err"):
        st.error(f"Error al enviar ventas: {st.session_state.admin_envio_err}")
    res = st.session_state.get("admin_envio")
    if isinstance(res, dict):
        nf = res.get("n_factura", 0)
        extra = f" · 🧾 {nf} como BORRADOR de factura (sin confirmar)" if nf else ""
        if res["simulado"]:
            st.info(f"🧪 SIMULACIÓN — se crearían **{res['n_ventas']}** pedido(s). "
                    f"No se escribió nada (ni en Odoo ni en la hoja).{extra}")
        else:
            st.success(f"📤 Envío ejecutado — {res['n_ventas']} pedido(s) procesado(s).{extra}")
        if not res["detalle"].empty:
            st.markdown("**Detalle de líneas:**")
            st.dataframe(res["detalle"], use_container_width=True, hide_index=True)
        if not res["resultados"].empty:
            st.markdown("**Resultado por venta:**")
            st.dataframe(res["resultados"], use_container_width=True, hide_index=True)

    st.divider()
    st.caption("Botón 3 (actualizar stock/carrito por pantalla) ya vive en la vista de ventas — se ejecuta "
               "al abrir, al presionar 'Actualizar' o al enviar una venta. El cron automático queda para más "
               "adelante si el tráfico lo amerita.")


# ---- Ruteo por token ----
if _qp_get("token") == TOKEN_ADMIN:
    render_admin()
    st.stop()


# ============================================================================
#  VISTA DE VENTAS  (default / ?token=expocafestore2026ventas)
# ============================================================================

# --- Estado de esta pestaña/dispositivo únicamente — no se comparte con otros ---
if "stock_df" not in st.session_state:
    refrescar()
if "carrito" not in st.session_state:
    st.session_state.carrito = []  # lista de dicts: sku, producto, cantidad, precio, subtotal


def agregar_al_carrito(sku, producto, precio, cantidad=1):
    for item in st.session_state.carrito:
        if item["sku"] == sku:
            item["cantidad"] += cantidad
            item["subtotal"] = item["cantidad"] * item["precio"]
            return
    st.session_state.carrito.append({
        "sku": sku, "producto": producto, "cantidad": cantidad,
        "precio": precio, "subtotal": precio * cantidad,
    })


# POS de vendedor: minimalista a propósito (carrito + grilla nomás) — clave en celular, donde
# el título y la descripción se comían casi toda la pantalla. El detalle (últimas ventas, totales
# por método de pago, resumen por dispositivo) vive solo en el panel admin (render_admin más arriba).

# Menos espacio muerto arriba (el default de Streamlit es grande) — pero sin invadir
# la barra fija superior (~60px, sidebar toggle + menú), o el encabezado queda tapado.
st.markdown("<style>.block-container{padding-top:4.5rem;}</style>", unsafe_allow_html=True)

# Identifica el dispositivo/caja: se toma de la URL (&caja=...) o, si no viene, de la barra lateral.
caja_id = _qp_get("caja")
if not caja_id:
    caja_id = st.sidebar.text_input(
        "🏷️ Nombre de esta caja / dispositivo",
        key="caja_id_input",
        help="Se guarda en cada venta y merma para saber qué dispositivo la registró. "
             "También puedes fijarlo en la URL: &caja=Caja1",
    )

col_cart, col_catalog = st.columns([1, 2])

# ---------------- CARRITO (izquierda) ----------------
with col_cart:
    # CSS del resumen compacto: total prominente + ítems chico en la misma banda; opciones/detalle van
    # en expanders colapsados para NO empujar el catálogo al agregar productos (pedido en móvil).
    st.markdown(
        "<style>"
        ".cart-sum{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:2px 2px 0}"
        ".cart-sum .cs-items{font-size:.85rem;color:#8b8b8b;white-space:nowrap}"
        ".cart-sum .cs-total{font-size:1.9rem;font-weight:800;line-height:1.05}"
        ".cart-desc{font-size:.78rem;color:#d98a2b;margin:0 0 2px;text-align:right}"
        ".st-key-opts_row div[data-testid=stHorizontalBlock]{flex-wrap:nowrap!important;gap:.5rem}"
        ".st-key-opts_row div[data-testid=stHorizontalBlock]>div{flex:1 1 0!important;min-width:0!important}"
        ".st-key-cart_detail_rows div[data-testid=stHorizontalBlock]{flex-wrap:nowrap!important;gap:.2rem;align-items:center}"
        ".st-key-cart_detail_rows div[data-testid=stHorizontalBlock]>div:first-child{flex:1 1 auto!important;min-width:0!important}"
        ".st-key-cart_detail_rows div[data-testid=stHorizontalBlock]>div:not(:first-child){flex:0 0 2.4rem!important;min-width:0!important}"
        ".st-key-cart_detail_rows .stButton>button{padding:.1rem 0!important;min-height:1.9rem}"
        "</style>",
        unsafe_allow_html=True,
    )
    st.caption(f"🏷️ {caja_id}" if caja_id else "🏷️ ⚠️ sin identificar — ver barra lateral")

    if not st.session_state.carrito:
        st.info("Carrito vacío. Toca un producto del catálogo para agregarlo.")
    else:
        carrito = st.session_state.carrito
        total_bruto = sum(item["subtotal"] for item in carrito)
        n_units = sum(item["cantidad"] for item in carrito)

        # --- init opciones ---
        opts_activas = (
            st.session_state.get("es_merma_venta", False)
            or st.session_state.get("factura_venta", False)
            or st.session_state.get("desc_tipo_venta", "Sin descuento") != "Sin descuento"
        )
        es_merma = False
        quiere_factura = False
        email_factura = ""
        desc_tipo = "Sin descuento"
        desc_val = 0

        # --- Opciones (izq) y Método de pago (der) en la MISMA fila (forzado horizontal en móvil) ---
        with st.container(key="opts_row"):
            col_o, col_m = st.columns([1, 1])
            with col_o:
                with st.expander("⚙️ Opciones", expanded=opts_activas):
                    es_merma = st.checkbox(
                        "📉 Merma (descuenta stock, no es venta)",
                        key="es_merma_venta",
                        help="Para productos rotos, derramados o perdidos. Descuenta stock igual que una venta, pero no cuenta como ingreso.",
                    )
                    if not es_merma:
                        quiere_factura = st.checkbox(
                            "🧾 Requiere factura",
                            key="factura_venta",
                            help="Registra el correo para contactar al cliente después. Entra a Odoo como borrador; la factura se emite a mano.",
                        )
                        if quiere_factura:
                            email_factura = st.text_input(
                                "Correo del cliente (factura)",
                                key="email_factura_venta", placeholder="cliente@empresa.cl",
                            )
                        desc_tipo = st.radio(
                            "Descuento",
                            ["Sin descuento", "Porcentaje (%)", "Monto ($)"],
                            key="desc_tipo_venta",
                        )
                        if desc_tipo == "Porcentaje (%)":
                            desc_val = st.number_input("% de descuento", min_value=0.0, max_value=100.0,
                                                       value=0.0, step=1.0, key="desc_pct_venta")
                        elif desc_tipo == "Monto ($)":
                            desc_val = st.number_input("Monto a descontar ($)", min_value=0,
                                                       value=0, step=500, key="desc_monto_venta")
            with col_m:
                if es_merma:
                    metodo_pago = "Merma"
                    st.caption("Merma — sin método de pago.")
                else:
                    metodo_pago = st.selectbox("Método de pago", METODOS_PAGO, key="metodo_pago_venta")

        # --- descuento calculado (aplica a la venta completa) ---
        descuento_monto = 0
        desc_label = ""
        if not es_merma and desc_val:
            if desc_tipo == "Porcentaje (%)":
                descuento_monto = int(round(total_bruto * float(desc_val) / 100))
                desc_label = f"{float(desc_val):g}%"
            elif desc_tipo == "Monto ($)":
                descuento_monto = int(desc_val)
                desc_label = money(int(desc_val))
        descuento_monto = max(0, min(descuento_monto, total_bruto))
        total_final = total_bruto - descuento_monto

        # --- resumen compacto SIEMPRE visible: líneas / productos + total ---
        n_lines = len(carrito)
        st.markdown(
            f"<div class='cart-sum'><span class='cs-items'>🛒 {n_lines} líneas / {int(n_units)} productos</span>"
            f"<span class='cs-total'>{money(total_final)}</span></div>",
            unsafe_allow_html=True,
        )
        if descuento_monto > 0:
            st.markdown(
                f"<div class='cart-desc'>antes {money(total_bruto)} · −{money(descuento_monto)} ({desc_label})</div>",
                unsafe_allow_html=True,
            )

        label_boton = "📉 Registrar merma" if es_merma else "✅ Enviar venta"
        if st.button(label_boton, type="primary", use_container_width=True):
            # Revalida contra datos frescos justo antes de escribir, para no sobrevender
            # si otro dispositivo vendió lo mismo mientras armabas el carrito.
            stock_fresco, ventas_fresco, ventas_ws = fetch_data()
            stock_fresco_idx = stock_fresco.set_index("SKU")
            problemas = []
            for item in carrito:
                restante = (
                    stock_fresco_idx.loc[item["sku"], "Stock restante"]
                    if item["sku"] in stock_fresco_idx.index else 0
                )
                if item["cantidad"] > restante:
                    problemas.append(f"**{item['producto']}**: quedan {int(restante)}, pediste {item['cantidad']}")

            st.session_state.stock_df = stock_fresco
            st.session_state.ventas_df = ventas_fresco
            st.session_state.ventas_ws = ventas_ws

            if quiere_factura and not email_factura.strip():
                problemas.append("**Falta el correo del cliente** — sin él no se puede emitir la factura después.")

            if problemas:
                st.error("Revisa antes de registrar:\n\n" + "\n\n".join(problemas))
            else:
                venta_id = datetime.now(CHILE_TZ).strftime("%Y%m%d%H%M%S")
                timestamp = datetime.now(CHILE_TZ).strftime("%Y-%m-%d %H:%M:%S")
                # El descuento de la venta se reparte proporcionalmente entre las líneas y se guarda el
                # Subtotal YA NETO. Como el sync a Odoo usa price_unit = Subtotal/Cantidad, Odoo recibe
                # el monto descontado, no el original. La última línea absorbe el redondeo (CLP entero).
                aplicar_desc = descuento_monto > 0 and total_bruto > 0 and not es_merma
                filas = []
                acc = 0
                n = len(carrito)
                for idx, item in enumerate(carrito):
                    if aplicar_desc:
                        d_i = (descuento_monto - acc) if idx == n - 1 else int(round(descuento_monto * item["subtotal"] / total_bruto))
                        acc += d_i
                        sub_net = max(0, item["subtotal"] - d_i)
                    else:
                        sub_net = item["subtotal"]
                    filas.append({
                        "Timestamp": timestamp, "SKU": item["sku"], "Producto": item["producto"],
                        "Cantidad": item["cantidad"], "Metodo_Pago": metodo_pago, "Subtotal": sub_net,
                        "Venta_ID": venta_id, "Merma": es_merma, "Dispositivo": caja_id,
                        "Factura": quiere_factura, "Email_Factura": email_factura.strip(),
                        "Descuento": desc_label if aplicar_desc else "",
                    })
                append_ventas(ventas_ws, filas)
                if es_merma:
                    st.success(f"📉 Merma registrada — {money(total_bruto)} en valor de referencia")
                elif quiere_factura:
                    st.success(f"🧾 Venta con factura registrada — Total {money(total_final)} · pendiente de emitir a {email_factura.strip()}")
                else:
                    extra = f" (desc. {desc_label})" if aplicar_desc else ""
                    st.success(f"✅ Venta registrada — Total {money(total_final)}{extra}")
                st.session_state.carrito = []
                refrescar()
                st.rerun()

        # --- Detalle del carrito: gestión inline (➕ ➖ 🗑️) compacta, colapsada por defecto ---
        with st.expander(f"📋 Ver / editar carrito ({n_lines} líneas · {int(n_units)} prod.)"):
            with st.container(key="cart_detail_rows"):
                stock_idx = st.session_state.stock_df.set_index("SKU")
                for i, item in enumerate(carrito):
                    c_n, c_p, c_m, c_d = st.columns([5, 1, 1, 1])
                    c_n.markdown(
                        f"**{item['producto']}**<br>"
                        f"<span style='color:#8b8b8b;font-size:.82rem'>x{item['cantidad']} · {money(item['subtotal'])}</span>",
                        unsafe_allow_html=True,
                    )
                    if c_p.button("➕", key=f"plus_{i}", help="Agregar uno"):
                        rest = stock_idx.loc[item["sku"], "Stock restante"] if item["sku"] in stock_idx.index else 0
                        if item["cantidad"] + 1 > rest:
                            st.toast(f"Solo quedan {int(rest)} de {item['producto']}")
                        else:
                            item["cantidad"] += 1
                            item["subtotal"] = item["cantidad"] * item["precio"]
                            st.rerun()
                    if c_m.button("➖", key=f"minus_{i}", help="Quitar uno"):
                        item["cantidad"] -= 1
                        if item["cantidad"] <= 0:
                            st.session_state.carrito.pop(i)
                        else:
                            item["subtotal"] = item["cantidad"] * item["precio"]
                        st.rerun()
                    if c_d.button("🗑️", key=f"del_{i}", help="Eliminar del carrito"):
                        st.session_state.carrito.pop(i)
                        st.rerun()

# ---------------- CATÁLOGO (derecha) ----------------
with col_catalog:
    head_cat_l, head_cat_r = st.columns([2, 1.3])
    head_cat_l.subheader("Catálogo")
    with head_cat_r:
        if st.button("🔄 Actualizar", use_container_width=True):
            refrescar()
            st.rerun()
    busqueda = st.text_input("🔎 Buscar producto...", key="busqueda_catalogo")

    en_carrito = {}
    for item in st.session_state.carrito:
        en_carrito[item["sku"]] = en_carrito.get(item["sku"], 0) + item["cantidad"]

    catalogo = st.session_state.stock_df.copy()
    catalogo["disponible_ahora"] = catalogo.apply(
        lambda r: r["Stock restante"] - en_carrito.get(r["SKU"], 0), axis=1
    )
    if busqueda:
        catalogo = catalogo[catalogo["Producto"].str.contains(busqueda, case=False, na=False)]

    catalogo_disponible = catalogo[catalogo["disponible_ahora"] > 0]
    catalogo_agotado = catalogo[catalogo["disponible_ahora"] <= 0]

    # La tarjeta completa es el botón (estilo Odoo POS) — sin botón "Agregar" aparte.
    # Estilo acotado al grid del catálogo vía el key del container (no toca otros botones de la página).
    st.markdown("""
        <style>
        .st-key-catalogo_grid div.stButton > button {
            text-align: left !important;
            justify-content: flex-start !important;
            white-space: normal !important;
            height: auto !important;
            min-height: 64px;
            padding: 8px 10px;
            line-height: 1.25;
        }
        .st-key-catalogo_grid div.stButton > button p {
            text-align: left !important;
            margin: 0;
            font-size: 0.85rem;
        }
        .st-key-catalogo_grid div.stButton > button p:first-child {
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        </style>
    """, unsafe_allow_html=True)

    with st.container(height=525, key="catalogo_grid"):
        if catalogo_disponible.empty and catalogo_agotado.empty:
            st.info("Sin resultados para esa búsqueda.")

        filas = list(catalogo_disponible.itertuples())
        for i in range(0, len(filas), COLS_GRID):
            cols = st.columns(COLS_GRID)
            for col, row in zip(cols, filas[i:i + COLS_GRID]):
                with col:
                    st.button(
                        f"**{row.Producto}**\n\n{money(row.Precio)} · quedan {int(row.disponible_ahora)}",
                        key=f"add_{row.SKU}", use_container_width=True,
                        on_click=agregar_al_carrito, args=(row.SKU, row.Producto, int(row.Precio)),
                    )

        if not catalogo_agotado.empty:
            st.caption(f"⛔ Sin stock ({len(catalogo_agotado)}): " + ", ".join(catalogo_agotado["Producto"].tolist()))
