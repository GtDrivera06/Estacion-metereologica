
# dashboard_meteo.py (Optimizado: cache, downsampling, ejes tiempo y redibujo inteligente)
import json, csv, requests, sqlite3, threading, time, math
from datetime import datetime
from collections import defaultdict

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.dates as mdates

# ================== CONFIG ==================
URL = "https://servidorestacionmeteorologica.onrender.com/lecturas"
DB_FILE = "lecturas_ui.db"
AUTO_REFRESH_SECONDS = 10

# Límite de filas en tabla y puntos en gráfico (tras downsampling)
MAX_ROWS_TABLE = 300
MAX_POINTS_CHART = 300

# Tamaño de ventana
WIN_GEOM = "1200x720"
# ============================================

# ----------- HTTP Session (keep-alive) -----------
_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": "MeteoDashboard/1.0"})
# -------------------------------------------------

# ----------------- DB utils -----------------
def _table_has_unique_on_lecturaid(conn):
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(lecturas_crudas)")
    cols = [r[1] for r in cur.fetchall()]
    if not cols:
        return False
    try:
        cur.execute("PRAGMA index_list(lecturas_crudas)")
        idxs = cur.fetchall()
        for _, name, unique, *_ in idxs:
            if int(unique) == 1:
                cur.execute(f"PRAGMA index_info({name})")
                cols = [r[2] for r in cur.fetchall()]
                if cols == ["lecturaId"]:
                    return True
    except Exception:
        pass
    return False

def db_init():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS lecturas_crudas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lecturaId INTEGER,
        valor REAL,
        timestamp TEXT,
        sensorNombre TEXT,
        tipoSensor TEXT,
        unidadMedicion TEXT,
        estacionNombre TEXT,
        estacionUbicacion TEXT,
        raw_json TEXT,
        UNIQUE(timestamp, estacionNombre, sensorNombre, unidadMedicion)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS lecturas_consolidadas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        fecha TEXT,
        hora TEXT,
        estacionNombre TEXT,
        temperatura REAL,
        presion REAL,
        altitud REAL,
        calidadAire REAL,
        UNIQUE(ts, estacionNombre)
    )""")
    conn.commit()

    # Migración si existía UNIQUE(lecturaId)
    if _table_has_unique_on_lecturaid(conn):
        try:
            c.execute("ALTER TABLE lecturas_crudas RENAME TO lecturas_crudas_old")
            conn.commit()
            c.execute("""
            CREATE TABLE lecturas_crudas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lecturaId INTEGER,
                valor REAL,
                timestamp TEXT,
                sensorNombre TEXT,
                tipoSensor TEXT,
                unidadMedicion TEXT,
                estacionNombre TEXT,
                estacionUbicacion TEXT,
                raw_json TEXT,
                UNIQUE(timestamp, estacionNombre, sensorNombre, unidadMedicion)
            )""")
            conn.commit()
            c.execute("""
            INSERT OR IGNORE INTO lecturas_crudas
            (lecturaId, valor, timestamp, sensorNombre, tipoSensor, unidadMedicion, estacionNombre, estacionUbicacion, raw_json)
            SELECT lecturaId, valor, timestamp, sensorNombre, tipoSensor, unidadMedicion, estacionNombre, estacionUbicacion, raw_json
            FROM lecturas_crudas_old
            """)
            conn.commit()
            c.execute("DROP TABLE lecturas_crudas_old")
            conn.commit()
        except Exception as e:
            print("Migración de DB falló:", e)
    conn.close()

def db_insert_raw(items):
    if not items:
        return 0
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    added = 0
    for it in items:
        try:
            c.execute("""
            INSERT OR IGNORE INTO lecturas_crudas
            (lecturaId, valor, timestamp, sensorNombre, tipoSensor, unidadMedicion, estacionNombre, estacionUbicacion, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                it.get("lecturaId"), it.get("valor"), it.get("timestamp"),
                it.get("sensorNombre"), it.get("tipoSensor"), it.get("unidadMedicion"),
                it.get("estacionNombre"), it.get("estacionUbicacion"),
                json.dumps(it, ensure_ascii=False)
            ))
            if c.rowcount > 0:
                added += 1
        except Exception as e:
            print("DB raw insert error:", e)
    conn.commit(); conn.close()
    return added

def db_insert_consolidated(rows):
    if not rows:
        return 0
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    added = 0
    for r in rows:
        try:
            c.execute("""
            INSERT OR IGNORE INTO lecturas_consolidadas
            (ts, fecha, hora, estacionNombre, temperatura, presion, altitud, calidadAire)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (r["ts"], r["fecha"], r["hora"], r["estacionNombre"],
                  r.get("temperatura"), r.get("presion"),
                  r.get("altitud"), r.get("calidadAire")))
            if c.rowcount > 0: added += 1
        except Exception as e:
            print("DB consolidated insert error:", e)
    conn.commit(); conn.close()
    return added

def db_fetch_raw(limit=MAX_ROWS_TABLE, est=None):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    if est:
        c.execute("""
        SELECT lecturaId, timestamp, estacionNombre, sensorNombre, tipoSensor, unidadMedicion, valor
        FROM lecturas_crudas
        WHERE estacionNombre = ?
        ORDER BY datetime(timestamp) DESC
        LIMIT ?""", (est, limit))
    else:
        c.execute("""
        SELECT lecturaId, timestamp, estacionNombre, sensorNombre, tipoSensor, unidadMedicion, valor
        FROM lecturas_crudas
        ORDER BY datetime(timestamp) DESC
        LIMIT ?""", (limit,))
    rows = c.fetchall(); conn.close()
    return rows[::-1]

def db_fetch_consolidated(limit=MAX_ROWS_TABLE, est=None):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    if est:
        c.execute("""
        SELECT fecha, hora, estacionNombre, temperatura, presion, altitud, calidadAire, ts
        FROM lecturas_consolidadas
        WHERE estacionNombre = ?
        ORDER BY datetime(ts) DESC
        LIMIT ?""", (est, limit))
    else:
        c.execute("""
        SELECT fecha, hora, estacionNombre, temperatura, presion, altitud, calidadAire, ts
        FROM lecturas_consolidadas
        ORDER BY datetime(ts) DESC
        LIMIT ?""", (limit,))
    rows = c.fetchall(); conn.close()
    return rows[::-1]

def db_fetch_estaciones():
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute("SELECT DISTINCT estacionNombre FROM lecturas_crudas ORDER BY estacionNombre ASC")
    rows = [r[0] for r in c.fetchall() if r[0]]
    conn.close(); return rows

def db_export_csv(path, est=None):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    if est:
        c.execute("""
        SELECT fecha, hora, estacionNombre, temperatura, presion, altitud, calidadAire, ts
        FROM lecturas_consolidadas
        WHERE estacionNombre = ?
        ORDER BY datetime(ts)""", (est,))
    else:
        c.execute("""
        SELECT fecha, hora, estacionNombre, temperatura, presion, altitud, calidadAire, ts
        FROM lecturas_consolidadas
        ORDER BY datetime(ts)""")
    rows = c.fetchall(); conn.close()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Fecha","Hora","Estacion","Temperatura(°C)","Presion(hPa)","Altitud(m)","CalidadAire(%)","Timestamp"])
        w.writerows(rows)
    return len(rows)

# --------------- Fetch & consolidate ---------------
def http_get_lecturas():
    r = _HTTP.get(URL, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("La respuesta no es una lista")
    return data

def consolidate(items):
    buckets = defaultdict(lambda: {"temperatura": None, "presion": None, "altitud": None, "calidadAire": None,
                                   "ts": None, "fecha": None, "hora": None, "estacionNombre": None})
    for it in items:
        ts = it.get("timestamp"); est = it.get("estacionNombre")
        if not ts or not est: continue
        b = buckets[(ts, est)]
        b["ts"] = ts; b["estacionNombre"] = est
        try:
            dt = datetime.fromisoformat(ts.replace("Z",""))
            b["fecha"] = dt.strftime("%Y-%m-%d"); b["hora"] = dt.strftime("%H:%M:%S")
        except Exception:
            b["fecha"] = ts[:10]; b["hora"] = ts[11:19] if len(ts) >= 19 else ""
        unidad = (it.get("unidadMedicion") or "").lower()
        tipo = (it.get("tipoSensor") or "").lower()
        sensor = (it.get("sensorNombre") or "").lower()
        val = it.get("valor")
        if unidad in ("°c","c","celsius"): b["temperatura"] = val
        elif unidad in ("hpa",): b["presion"] = val
        elif unidad in ("m",): b["altitud"] = val
        elif unidad in ("%",) or "calidad" in tipo or sensor == "mq8": b["calidadAire"] = val
    rows = list(buckets.values())
    rows.sort(key=lambda x: (x["ts"] or "", x["estacionNombre"] or ""))
    return rows

# ---------------- Utils: downsampling y formato tiempo ----------------
def thin_series(xs, ys, max_points=MAX_POINTS_CHART):
    """Reduce puntos manteniendo forma. Si xs>max_points, toma saltos equiespaciados."""
    n = len(xs)
    if n <= max_points:
        return xs, ys
    step = max(1, n // max_points)
    xs2 = xs[::step]
    ys2 = ys[::step]
    # Garantizar último punto
    if xs2[-1] != xs[-1]:
        xs2.append(xs[-1]); ys2.append(ys[-1])
    return xs2, ys2

def parse_ts_list(ts_list):
    """Convierte strings ISO a matplotlib datenums."""
    out = []
    for s in ts_list:
        try:
            dt = datetime.fromisoformat((s or "").replace("Z",""))
        except Exception:
            # fallback básico
            try:
                dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                dt = datetime.utcnow()
        out.append(mdates.date2num(dt))
    return out

# ---------------- UI (dark) ----------------
def configure_dark_theme(root):
    root.configure(bg="#0e0f11")
    style = ttk.Style(); style.theme_use("clam")
    PA = {"bg":"#0e0f11","panel":"#15171a","panel2":"#1b1e22","fg":"#d4d7dd","accent":"#00bcd4","muted":"#9aa0a6","grid":"#23262a"}
    style.configure("TFrame", background=PA["bg"])
    style.configure("Panel.TFrame", background=PA["panel"])
    style.configure("Panel2.TFrame", background=PA["panel2"])
    style.configure("TLabel", background=PA["bg"], foreground=PA["fg"])
    style.configure("Header.TLabel", background=PA["bg"], foreground=PA["fg"], font=("Segoe UI",16,"bold"))
    style.configure("Muted.TLabel", background=PA["bg"], foreground=PA["muted"])
    style.configure("Accent.TLabel", background=PA["bg"], foreground=PA["accent"], font=("Segoe UI",12,"bold"))
    style.configure("TButton", background=PA["panel2"], foreground=PA["fg"], borderwidth=0)
    style.map("TButton", background=[("active", PA["accent"])])
    style.configure("Treeview", background=PA["panel"], fieldbackground=PA["panel"], foreground=PA["fg"],
                    bordercolor=PA["grid"], borderwidth=0)
    style.configure("Treeview.Heading", background=PA["panel2"], foreground=PA["fg"], relief="flat")
    style.map("Treeview.Heading", background=[("active", PA["accent"])])

class DashboardApp:
    def __init__(self, root):
        self.root = root; self.root.title("Estación Meteorológica — Dashboard (Modo Oscuro)")
        configure_dark_theme(root)
        self.auto = False; self.selected_station = tk.StringVar(value="(Todas)")
        self.status_var = tk.StringVar(value="Listo.")
        self._last_hash_conso = None   # para redibujo inteligente

        # Top bar
        top = ttk.Frame(root, style="Panel2.TFrame"); top.pack(fill=tk.X, padx=10, pady=8)
        ttk.Label(top, text="Dashboard Meteorológico", style="Header.TLabel").pack(side=tk.LEFT, padx=(8,24))
        ttk.Label(top, text="Estación:", style="Muted.TLabel").pack(side=tk.LEFT)
        self.station_cb = ttk.Combobox(top, textvariable=self.selected_station, state="readonly", width=30)
        self.station_cb.pack(side=tk.LEFT, padx=6); self.station_cb["values"] = ["(Todas)"]
        self.station_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_all())
        ttk.Button(top, text="Refrescar", command=self.manual_refresh).pack(side=tk.LEFT, padx=6)
        self.auto_btn = ttk.Button(top, text="Iniciar Auto-Refresh", command=self.toggle_auto); self.auto_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Exportar CSV", command=self.export_csv).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Limpiar caché", command=self.clear_cache).pack(side=tk.LEFT, padx=6)
        ttk.Label(top, textvariable=self.status_var, style="Muted.TLabel").pack(side=tk.RIGHT, padx=8)

        # Cards
        cards = ttk.Frame(root, style="Panel.TFrame"); cards.pack(fill=tk.X, padx=10, pady=(0,8))
        self.card_temp = self._make_card(cards, "Temperatura (°C)")
        self.card_press = self._make_card(cards, "Presión (hPa)")
        self.card_alt = self._make_card(cards, "Altitud (m)")
        self.card_air = self._make_card(cards, "Calidad Aire (%)")
        for w in (self.card_temp, self.card_press, self.card_alt, self.card_air):
            w.pack(side=tk.LEFT, expand=True, fill=tk.BOTH, padx=6, pady=6)

        # Layout bottom
        mid = ttk.Frame(root); mid.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        left = ttk.Notebook(mid); left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0,6))
        self.tree_conso_frame, self.tree_conso = self._make_tree(left, ("Fecha","Hora","Estación","Temp(°C)","Pres(hPa)","Alt(m)","Aire(%)","ts"))
        self.tree_raw_frame, self.tree_raw = self._make_tree(left, ("lecturaId","timestamp","Estación","Sensor","Tipo","Unidad","Valor"))
        left.add(self.tree_conso_frame, text="Lecturas Consolidadas")
        left.add(self.tree_raw_frame, text="Lecturas Crudas")

        right = ttk.Notebook(mid); right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6,0))
        self.fig = Figure(figsize=(6,5), dpi=100, facecolor="#0e0f11")
        self.ax_temp = self.fig.add_subplot(411); self.ax_press = self.fig.add_subplot(412)
        self.ax_alt = self.fig.add_subplot(413); self.ax_air = self.fig.add_subplot(414)
        self._prepare_axis(self.ax_temp, "°C")
        self._prepare_axis(self.ax_press, "hPa")
        self._prepare_axis(self.ax_alt, "m")
        self._prepare_axis(self.ax_air, "%")
        self.ax_air.set_xlabel("Tiempo", color="#d4d7dd")

        # formateo de fecha en X
        for ax in (self.ax_temp, self.ax_press, self.ax_alt, self.ax_air):
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M\n%d-%m"))
            ax.tick_params(axis="x", labelrotation=0)

        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        right.add(self.canvas.get_tk_widget(), text="Gráficos (Tiempo)")

        self.refresh_all(initial=True)

    # helpers UI
    def _make_card(self, parent, title):
        f = ttk.Frame(parent, style="Panel2.TFrame", padding=12)
        ttk.Label(f, text=title, style="Muted.TLabel").pack(anchor="w")
        lbl = ttk.Label(f, text="—", style="Accent.TLabel"); lbl.pack(anchor="w", pady=(6,0))
        f.value_label = lbl; return f

    def _make_tree(self, parent, columns):
        frame = ttk.Frame(parent, style="Panel.TFrame")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=16)
        for c in columns: tree.heading(c, text=c); tree.column(c, width=110, anchor=tk.CENTER)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview); tree.configure(yscroll=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True); vsb.pack(side=tk.LEFT, fill=tk.Y)
        return frame, tree

    # estética de ejes (modo oscuro) — se aplica tras cada cla()
    def _prepare_axis(self, ax, ylabel):
        ax.grid(True, alpha=0.25)
        ax.set_facecolor("#15171a")
        for s in ax.spines.values(): s.set_color("#23262a")
        ax.tick_params(colors="#d4d7dd", labelsize=8)
        ax.xaxis.label.set_color("#d4d7dd")
        ax.yaxis.label.set_color("#d4d7dd")
        ax.set_ylabel(ylabel)

    # actions
    def set_status(self, msg): self.status_var.set(msg); self.root.update_idletasks()
    def manual_refresh(self): threading.Thread(target=self._refresh_worker, daemon=True).start()
    def toggle_auto(self):
        self.auto = not self.auto
        self.auto_btn.configure(text="Detener Auto-Refresh" if self.auto else "Iniciar Auto-Refresh")
        if self.auto: threading.Thread(target=self._auto_worker, daemon=True).start()

    def _auto_worker(self):
        while self.auto:
            try: self._refresh_worker()
            except Exception as e: print("auto refresh error:", e)
            for _ in range(AUTO_REFRESH_SECONDS):
                if not self.auto: break
                time.sleep(1)

    def _refresh_worker(self):
        try:
            self.set_status("Consultando servidor…")
            items = http_get_lecturas()
            added_raw = db_insert_raw(items)
            rows = consolidate(items)
            added_conso = db_insert_consolidated(rows)
            self.set_status(f"OK · Crudas +{added_raw} · Consolidadas +{added_conso}")
        except Exception as e:
            self.set_status(f"Error: {e}")
        finally:
            self.refresh_all()

    def refresh_all(self, initial=False):
        stations = ["(Todas)"] + db_fetch_estaciones()
        cur = self.selected_station.get()
        self.station_cb["values"] = stations
        if cur not in stations: self.selected_station.set("(Todas)")
        est = None if self.selected_station.get() == "(Todas)" else self.selected_station.get()

        # Tablas
        self._fill_tree(self.tree_conso, db_fetch_consolidated(limit=MAX_ROWS_TABLE, est=est))
        self._fill_tree(self.tree_raw, db_fetch_raw(limit=MAX_ROWS_TABLE, est=est))

        # Tarjetas + gráficas (solo si cambió dataset)
        self.update_cards_and_charts(est)

    def _fill_tree(self, tree, rows):
        tree.delete(*tree.get_children())
        for r in rows: tree.insert("", tk.END, values=r)

    def export_csv(self):
        est = None if self.selected_station.get() == "(Todas)" else self.selected_station.get()
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
        if not path: return
        n = db_export_csv(path, est=est)
        messagebox.showinfo("Exportación", f"Exportadas {n} filas a:\n{path}")

    def clear_cache(self):
        if not messagebox.askyesno("Confirmar", "¿Borrar TODA la base local (cache) y recargar?"):
            return
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        c.execute("DELETE FROM lecturas_crudas"); c.execute("DELETE FROM lecturas_consolidadas")
        conn.commit(); conn.close()
        self.set_status("Caché limpiada. Pulsa Refrescar.")

    def _hash_rows(self, rows):
        # hash simple por última marca de tiempo + cantidad
        if not rows: return "0|0"
        last_ts = rows[-1][7] if len(rows[-1]) > 7 else ""
        return f"{len(rows)}|{last_ts}"

    def update_cards_and_charts(self, est):
        rows = db_fetch_consolidated(limit=MAX_POINTS_CHART*3, est=est)  # traemos un poco más y reducimos
        # Redibujo inteligente (si no cambió, salimos)
        h = self._hash_rows(rows)
        if h == self._last_hash_conso:
            return
        self._last_hash_conso = h

        # Tarjetas
        if rows:
            last = rows[-1]; _,_,_, temp,pres,alt,air,_ = last
            self.card_temp.value_label.configure(text=f"{temp if temp is not None else '—'}")
            self.card_press.value_label.configure(text=f"{pres if pres is not None else '—'}")
            self.card_alt.value_label.configure(text=f"{alt if alt is not None else '—'}")
            self.card_air.value_label.configure(text=f"{air if air is not None else '—'}")
        else:
            for c in (self.card_temp, self.card_press, self.card_alt, self.card_air):
                c.value_label.configure(text="—")

        # Gráficas: limpiar y re-preparar ejes
        for ax in (self.ax_temp, self.ax_press, self.ax_alt, self.ax_air):
            ax.cla()
        self._prepare_axis(self.ax_temp, "°C")
        self._prepare_axis(self.ax_press, "hPa")
        self._prepare_axis(self.ax_alt, "m")
        self._prepare_axis(self.ax_air, "%")
        for ax in (self.ax_temp, self.ax_press, self.ax_alt, self.ax_air):
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M\n%d-%m"))
            ax.tick_params(axis="x", labelrotation=0)
        self.ax_air.set_xlabel("Tiempo", color="#d4d7dd")

        if not rows:
            self.fig.tight_layout(pad=1.2); self.canvas.draw_idle()
            return

        # Construcción de series
        if est is None:
            by_est = defaultdict(list)
            for r in rows: by_est[r[2]].append(r)
            for estname, serie in by_est.items():
                ts = [s[7] for s in serie]
                t = parse_ts_list(ts)
                temp = [s[3] for s in serie]; pres = [s[4] for s in serie]
                alt  = [s[5] for s in serie]; air  = [s[6] for s in serie]

                # downsampling
                t, temp = thin_series(t, temp)
                _, pres = thin_series(t, pres)  # usar mismos índices tras el primer thin
                _, alt  = thin_series(t, alt)
                _, air  = thin_series(t, air)

                self.ax_temp.plot(t, temp, marker=".", linewidth=1.2, label=estname)
                self.ax_press.plot(t, pres, marker=".", linewidth=1.2, label=estname)
                self.ax_alt.plot(t, alt, marker=".", linewidth=1.2, label=estname)
                self.ax_air.plot(t, air, marker=".", linewidth=1.2, label=estname)
            self.ax_temp.legend(loc="upper left", fontsize=8)
        else:
            ts = [r[7] for r in rows]
            t = parse_ts_list(ts)
            temp = [r[3] for r in rows]; pres = [r[4] for r in rows]
            alt  = [r[5] for r in rows]; air  = [r[6] for r in rows]

            t, temp = thin_series(t, temp)
            _, pres = thin_series(t, pres)
            _, alt  = thin_series(t, alt)
            _, air  = thin_series(t, air)

            self.ax_temp.plot(t, temp, marker=".", linewidth=1.4, label=est or "")
            self.ax_press.plot(t, pres, marker=".", linewidth=1.4, label=est or "")
            self.ax_alt.plot(t, alt, marker=".", linewidth=1.4, label=est or "")
            self.ax_air.plot(t, air, marker=".", linewidth=1.4, label=est or "")

        # límites ajustados
        for ax in (self.ax_temp, self.ax_press, self.ax_alt, self.ax_air):
            ax.relim(); ax.autoscale_view()

        self.fig.tight_layout(pad=1.2); self.canvas.draw_idle()

def main():
    db_init()
    root = tk.Tk()
    app = DashboardApp(root)
    root.geometry(WIN_GEOM); root.minsize(1060, 620)
    root.mainloop()

if __name__ == "__main__":
    main()
