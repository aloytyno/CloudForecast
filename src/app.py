"""
PilviEnnuste — main application window.

Layout
------
Left  : Zoomable/clickable TkinterMapView centred on Finland.
Right : Matplotlib step-area chart of the 5-day TotalCloudCover forecast
        for the last clicked coordinate, expressed in oktas (0–8).
        Hover the mouse over the chart to see a datatip.
"""

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import customtkinter as ctk
import matplotlib.dates as mdates
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from PIL import Image, ImageDraw
from PIL.ImageTk import PhotoImage
from tkintermapview import TkinterMapView

from src.fmi_client import get_cloud_cover_forecast


# ── Geography ──────────────────────────────────────────────────────────────
FINLAND_LAT = 60.85
FINLAND_LON = 25.0
FINLAND_ZOOM = 7
HELSINKI_LAT = 60.1699
HELSINKI_LON = 24.9384
HELSINKI_TZ = ZoneInfo("Europe/Helsinki")

# ── Palette (matches CTk dark theme) ──────────────────────────────────────
BG = "#1c1c1e"
SURFACE = "#2c2c2e"
SURFACE2 = "#3a3a3c"
ACCENT = "#0a84ff"
TEXT = "#f2f2f7"
SUBTEXT = "#8e8e93"
CLOUD_FILL = "#5ac8fa"
CLOUD_LINE = "#007aff"
DANGER = "#ff453a"
GRID = "#3a3a3c"

# Map overlay colours indexed by okta (0–8): RGBA, cloud-like blue-grey
_OKTA_RGBA = [
    (200, 220, 240,   0),   # 0  – clear, fully transparent
    (200, 220, 240,  20),   # 1
    (190, 210, 235,  42),   # 2
    (180, 200, 230,  68),   # 3
    (170, 190, 225,  98),   # 4
    (160, 180, 215, 128),   # 5
    (150, 165, 205, 158),   # 6
    (140, 150, 195, 185),   # 7
    (130, 140, 185, 210),   # 8  – overcast, ~82 % opaque
]

# Okta category background bands  (ymin, ymax, label, colour)
OKTA_BANDS = [
    (0, 2, "Clear",        "#4ade80"),
    (2, 5, "Partly cloudy","#fbbf24"),
    (5, 7, "Mostly cloudy","#94a3b8"),
    (7, 8, "Overcast",     "#64748b"),
]


def _night_durations(lat: float, lon: float, target_date):
    """Return night info for target_date (local date) at two thresholds.

    Returns a dict keyed by threshold (-12, -18), each value is
    (duration_mins, start_datetime_or_None, end_datetime_or_None).
    Sampled at 5-minute intervals; crossings interpolated linearly.
    """
    from astral import LocationInfo
    from astral.sun import elevation as sun_elev

    observer = LocationInfo(latitude=lat, longitude=lon).observer
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=HELSINKI_TZ)
    # 289 samples: indices 0–288 cover 00:00–24:00 with 5-min spacing
    ts_list = [day_start + timedelta(minutes=step * 5) for step in range(289)]
    alts = []
    for ts in ts_list:
        try:
            alts.append(sun_elev(observer, ts.astimezone(timezone.utc)))
        except Exception:
            alts.append(-90.0)

    result = {}
    for thr in (-12, -18):
        mins = sum(1 for a in alts[:-1] if a < thr) * 5

        start_dt = None
        for i in range(len(alts) - 1):
            if alts[i] >= thr and alts[i + 1] < thr:
                frac = (thr - alts[i]) / (alts[i + 1] - alts[i])
                start_dt = ts_list[i] + timedelta(minutes=frac * 5)
                break

        end_dt = None
        for i in range(len(alts) - 2, -1, -1):
            if alts[i] < thr and alts[i + 1] >= thr:
                frac = (thr - alts[i]) / (alts[i + 1] - alts[i])
                end_dt = ts_list[i] + timedelta(minutes=frac * 5)
                break

        result[thr] = (mins, start_dt, end_dt)
    return result


def _altitude_color(alt_deg: float) -> tuple[float, float, float]:
    """Map solar altitude (degrees) to an RGB colour for the chart background.

    Colour stops:
      < -18  deep night (near black)
      -18    start of astronomical twilight (dark navy)
      -12    nautical twilight (dark blue)
       -6    blue hour peak (rich blue)
       -2    civil twilight edge (warm orange transition)
        0    sunrise / sunset (orange)
        5    golden hour (amber)
       15    morning / afternoon sky (sky blue)
       35    higher sun (light blue)
       70    bright midday
    """
    _STOPS = [
        (-90,  0.015, 0.015, 0.040),
        (-18,  0.028, 0.042, 0.110),
        (-12,  0.040, 0.070, 0.220),
        ( -6,  0.055, 0.115, 0.400),
        ( -2,  0.680, 0.260, 0.055),
        (  0,  0.760, 0.380, 0.060),
        (  5,  0.800, 0.580, 0.080),
        ( 15,  0.420, 0.660, 0.820),
        ( 35,  0.560, 0.760, 0.880),
        ( 70,  0.680, 0.840, 0.920),
    ]
    if alt_deg <= _STOPS[0][0]:
        return _STOPS[0][1:]
    if alt_deg >= _STOPS[-1][0]:
        return _STOPS[-1][1:]
    for i in range(len(_STOPS) - 1):
        a0, r0, g0, b0 = _STOPS[i]
        a1, r1, g1, b1 = _STOPS[i + 1]
        if a0 <= alt_deg <= a1:
            t = (alt_deg - a0) / (a1 - a0)
            return (r0 + t*(r1-r0), g0 + t*(g1-g0), b0 + t*(b1-b0))
    return _STOPS[-1][1:]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("PilviEnnuste – Cloud Cover Forecast")
        self.geometry("1600x780")
        self.minsize(1100, 600)
        self.configure(fg_color=BG)

        self._marker = None
        self._marker_icon = self._make_marker_icon()
        self._fetch_thread: threading.Thread | None = None
        self._plot_times: list | None = None   # Helsinki-tz datetimes
        self._plot_oktas: list | None = None   # raw oktas (−1 = missing)
        self._xlim_full: tuple | None = None   # full x range as mpl floats
        self._pan_start: tuple | None = None   # (xdata_float, (xmin, xmax))
        self._slider_vline = None              # vertical line on chart for slider time

        # Map overlay state
        self._grid_forecast = None             # GridForecast once loaded
        self._slider_time: datetime | None = None
        self._overlay_photo = None             # keep PhotoImage reference (prevent GC)
        self._overlay_canvas_id: int | None = None
        self._overlay_map_state = None         # (zoom, ul_tile, lr_tile) at last render
        self._map_ts_ids: list[int] = []       # canvas item ids for timestamp overlay
        self._render_after_id: str | None = None   # debounce handle

        self._build_ui()
        self._start_grid_fetch()
        self.after(600, self._poll_map_state)
        self.after(200, lambda: self._on_map_click((HELSINKI_LAT, HELSINKI_LON)))

    # ── Layout ────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=5)   # map
        self.grid_columnconfigure(1, weight=5)   # graph (equal share of extra width)
        self.grid_rowconfigure(0, weight=1)

        self._build_map_panel()
        self._build_graph_panel()

    def _build_map_panel(self):
        frame = ctk.CTkFrame(self, corner_radius=14, fg_color=SURFACE)
        frame.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)   # row 1 = map expands
        # row 2 = slider (fixed height)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        #ctk.CTkLabel(
        #    header, text="Finland",
        #    font=ctk.CTkFont(size=18, weight="bold"), text_color=TEXT,
        #).pack(side="left")
        ctk.CTkLabel(
            header, text="  Click anywhere to fetch forecast",
            font=ctk.CTkFont(size=14), text_color=SUBTEXT,
        ).pack(side="left")

        self._map = TkinterMapView(frame, corner_radius=10, bg_color=SURFACE)
        self._map.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 4))
        self._map.set_position(FINLAND_LAT, FINLAND_LON)
        self._map.set_zoom(FINLAND_ZOOM)
        self._map.add_left_click_map_command(self._on_map_click)

        # Clear overlay immediately on zoom so stale image doesn't sit under new tiles
        self._map.canvas.bind("<MouseWheel>",  self._on_map_zoom_event, "+")
        self._map.canvas.bind("<Button-4>",    self._on_map_zoom_event, "+")
        self._map.canvas.bind("<Button-5>",    self._on_map_zoom_event, "+")

        # ── Time slider row ────────────────────────────────────────────────
        slider_row = ctk.CTkFrame(frame, fg_color="transparent")
        slider_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        slider_row.grid_columnconfigure(1, weight=1)

        self._time_label = ctk.CTkLabel(
            slider_row,
            text="Loading map data…",
            font=ctk.CTkFont(size=11),
            text_color=SUBTEXT,
            width=148,
            anchor="w",
        )
        self._time_label.grid(row=0, column=0, padx=(0, 8))

        self._time_slider = ctk.CTkSlider(
            slider_row,
            from_=0, to=1,
            number_of_steps=1,
            command=self._on_slider_change,
            state="disabled",
            button_color=ACCENT,
            button_hover_color=CLOUD_LINE,
            progress_color=SURFACE2,
            fg_color=SURFACE2,
        )
        self._time_slider.grid(row=0, column=1, sticky="ew")

    def _build_graph_panel(self):
        frame = ctk.CTkFrame(self, corner_radius=14, fg_color=SURFACE)
        frame.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        self._loc_label = ctk.CTkLabel(
            frame, text="No location selected",
            font=ctk.CTkFont(size=17, weight="bold"), text_color=TEXT,
        )
        self._loc_label.grid(row=0, column=0, sticky="w", padx=14, pady=(14, 2))

        self._status_label = ctk.CTkLabel(
            frame, text="Click on the map to see the cloudiness forecast",
            font=ctk.CTkFont(size=12), text_color=SUBTEXT,
        )
        self._status_label.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 8))

        self._fig = Figure(facecolor=SURFACE)
        self._fig.subplots_adjust(left=0.08, right=0.78, top=0.83, bottom=0.20)
        self._ax = self._fig.add_subplot(111, facecolor=BG)

        self._canvas = FigureCanvasTkAgg(self._fig, master=frame)
        widget = self._canvas.get_tk_widget()
        widget.configure(bg=SURFACE, highlightthickness=0)
        widget.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))

        # Hover, zoom, pan — connected once; artists recreated per plot update
        self._annot = None
        self._highlight = None
        self._canvas.mpl_connect("motion_notify_event",   self._on_hover)
        self._canvas.mpl_connect("axes_leave_event",      self._on_axes_leave)
        self._canvas.mpl_connect("scroll_event",          self._on_scroll)
        self._canvas.mpl_connect("button_press_event",    self._on_button_press)
        self._canvas.mpl_connect("button_release_event",  self._on_button_release)
        self._canvas.mpl_connect("draw_event",            self._on_after_draw)

        self._draw_empty_plot()

    # ── Plot helpers ──────────────────────────────────────────────────────

    def _style_axes(self):
        ax = self._ax
        ax.set_facecolor("#000000")
        ax.tick_params(colors=SUBTEXT, labelsize=9)
        ax.set_ylabel("Cloud cover (oktas)", color=SUBTEXT, fontsize=10, labelpad=6)
        ax.set_xlabel("Helsinki time", color=SUBTEXT, fontsize=10, labelpad=6)
        ax.set_ylim(0, 8.3)
        ax.set_yticks(range(9))
        ax.set_yticklabels([str(i) for i in range(9)], color=SUBTEXT, fontsize=9)
        for spine in ax.spines.values():
            spine.set_color(GRID)
        ax.grid(axis="both", color=GRID, linestyle=":", linewidth=0.8, zorder=3)

    def _make_highlight(self):
        """Create a highlight circle artist (hidden until hover)."""
        return self._ax.plot(
            [], [], "o",
            color=ACCENT, markersize=9,
            markerfacecolor="none", markeredgewidth=2,
            zorder=6, visible=False,
        )[0]

    def _make_annot(self):
        """Create a fresh annotation object on the current axes."""
        return self._ax.annotate(
            "",
            xy=(0, 0),
            xytext=(14, 14),
            textcoords="offset points",
            bbox=dict(
                boxstyle="round,pad=0.5",
                fc=SURFACE2, ec=ACCENT, lw=1.4, alpha=0.95,
            ),
            color=TEXT,
            fontsize=9,
            linespacing=1.6,
            visible=False,
            zorder=10,
        )

    def _draw_empty_plot(self):
        ax = self._ax
        ax.clear()
        self._style_axes()
        ax.set_xticks([])
        ax.text(
            0.5, 0.5, "Select a location on the map",
            transform=ax.transAxes, ha="center", va="center",
            color=SUBTEXT, fontsize=13, alpha=0.7,
        )
        self._annot = None
        self._highlight = None
        self._canvas.draw_idle()

    def _draw_loading_plot(self, lat: float, lon: float):
        ax = self._ax
        ax.clear()
        self._style_axes()
        ax.set_xticks([])
        ax.text(
            0.5, 0.5, f"Fetching forecast for\n{lat:.4f}°N  {lon:.4f}°E ...",
            transform=ax.transAxes, ha="center", va="center",
            color=SUBTEXT, fontsize=12, linespacing=1.8,
        )
        self._annot = None
        self._highlight = None
        self._canvas.draw_idle()

    @staticmethod
    def _make_marker_icon(size: int = 14) -> PhotoImage:
        """Create a small red circle for use as a map marker icon."""
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([0, 0, size - 1, size - 1], fill="white")
        draw.ellipse([2, 2, size - 3, size - 3], fill="#e53935")
        return PhotoImage(img)

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_map_click(self, coords: tuple[float, float]):
        lat, lon = coords

        if self._marker:
            self._marker.delete()
        self._marker = self._map.set_marker(
            lat, lon,
            text=f"{lat:.3f}°N  {lon:.3f}°E",
            icon=self._marker_icon,
            icon_anchor="center",
        )

        self._loc_label.configure(text=f"{lat:.4f}°N,  {lon:.4f}°E")
        self._status_label.configure(text="Fetching...")
        self._draw_loading_plot(lat, lon)

        self._fetch_thread = threading.Thread(
            target=self._fetch_and_update, args=(lat, lon), daemon=True,
        )
        self._fetch_thread.start()

    def _fetch_and_update(self, lat: float, lon: float):
        try:
            data = get_cloud_cover_forecast(lat, lon, hours_ahead=120)
            self.after(0, self._update_plot, data, lat, lon)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._show_error, str(exc))

    def _on_hover(self, event):
        # ── Pan (left-click drag) ─────────────────────────────────────────
        if (self._pan_start is not None
                and event.xdata is not None
                and self._xlim_full is not None):
            x0, (xmin0, xmax0) = self._pan_start
            dx = x0 - event.xdata
            span = xmax0 - xmin0
            full_min, full_max = self._xlim_full
            new_xmin = xmin0 + dx
            new_xmax = xmax0 + dx
            if new_xmin < full_min:
                new_xmin, new_xmax = full_min, full_min + span
            if new_xmax > full_max:
                new_xmin, new_xmax = full_max - span, full_max
            self._ax.set_xlim(new_xmin, new_xmax)
            self._canvas.draw_idle()
            return   # suppress datatip while panning

        # ── Datatip ───────────────────────────────────────────────────────
        if (event.inaxes is not self._ax
                or self._annot is None
                or self._plot_times is None
                or event.xdata is None):
            return

        # Find the nearest data point to the cursor x position
        cursor_dt = mdates.num2date(event.xdata).astimezone(HELSINKI_TZ)
        idx = min(
            range(len(self._plot_times)),
            key=lambda i: abs((self._plot_times[i] - cursor_dt).total_seconds()),
        )
        ts  = self._plot_times[idx]
        ok  = self._plot_oktas[idx]

        label = ts.strftime("%a %d.%m.  %H:%M")
        if ok >= 0:
            label += f"\n{ok}/8 oktas"
        else:
            label += "\nNo data"

        # Position the tip above the bar; flip to left side near right edge
        x_num = mdates.date2num(ts)
        y_val = max(ok, 0)
        x_frac = (x_num - self._ax.get_xlim()[0]) / (self._ax.get_xlim()[1] - self._ax.get_xlim()[0])
        offset = (-90, 14) if x_frac > 0.8 else (14, 14)

        self._annot.xy = (x_num, y_val)
        self._annot.set_text(label)
        self._annot.xyann = offset
        self._annot.set_visible(True)

        if self._highlight is not None:
            self._highlight.set_data([x_num], [y_val])
            self._highlight.set_visible(True)

        self._canvas.draw_idle()

    def _on_axes_leave(self, _event):
        changed = False
        if self._annot is not None:
            self._annot.set_visible(False)
            changed = True
        if self._highlight is not None:
            self._highlight.set_visible(False)
            changed = True
        if changed:
            self._canvas.draw_idle()

    def _draw_sun_background(self, lat, lon, t_start, t_end, ax):
        """Fill the axes background with sky colours based on solar altitude,
        and draw dotted vertical lines at astronomical night boundaries (±18°)."""
        from astral import LocationInfo
        from astral.sun import elevation as sun_elev

        observer = LocationInfo(latitude=lat, longitude=lon).observer
        strip = timedelta(minutes=20)

        # Build a list of boundary times at 20-min resolution
        segs = []
        t = t_start
        while t < t_end:
            segs.append(t)
            t += strip
        segs.append(t_end)

        # Elevation at each boundary
        alts = []
        for ts in segs:
            try:
                alts.append(sun_elev(observer, ts.astimezone(timezone.utc)))
            except Exception:
                alts.append(-90.0)

        # Coloured strips (zorder=0, below everything)
        for i in range(len(segs) - 1):
            avg_alt = (alts[i] + alts[i + 1]) / 2
            ax.axvspan(segs[i], segs[i + 1],
                       facecolor=_altitude_color(avg_alt),
                       alpha=0.60, zorder=0, linewidth=0)


    # ── Plot update ───────────────────────────────────────────────────────

    def _update_plot(self, data: list[tuple], lat: float, lon: float):
        if not data:
            self._show_error("No forecast data returned.\nThis location may be outside FMI coverage.")
            return

        times = [ts.astimezone(HELSINKI_TZ) for ts, _ in data]
        oktas = [ok for _, ok in data]

        # Extend the last step so the final bar has visible width
        last_step = (times[-1] - times[-2]).total_seconds() if len(times) > 1 else 3600
        times_ext = times + [times[-1] + timedelta(seconds=last_step)]
        oktas_ext = [float("nan") if o < 0 else o for o in oktas + [oktas[-1]]]

        ax = self._ax
        ax.clear()
        self._style_axes()

        # Sky-light background (zorder 0) + astronomical night lines (zorder 4)
        self._draw_sun_background(lat, lon, times[0], times[-1], ax)

        # Night-duration title (two lines above the chart)
        tomorrow = (datetime.now(HELSINKI_TZ) + timedelta(days=1)).date()
        night_info = _night_durations(lat, lon, tomorrow)

        def _fmt_night(thr: int) -> str:
            mins, start_dt, end_dt = night_info[thr]
            if mins == 0:
                return "none (midnight sun)"
            dur = f"{mins // 60}h {mins % 60:02d}min"
            if start_dt and end_dt:
                s = start_dt.strftime("%H:%M")
                e = end_dt.strftime("%H:%M")
                return f"{dur}  ({s} – {e})"
            return dur

        ax.set_title(
            f"Astronomical twilight night: {_fmt_night(-12)}\n"
            f"Fully dark night: {_fmt_night(-18)}",
            color=SUBTEXT, fontsize=10.5, loc="left", pad=5,
        )

        # Okta category bands (zorder 1)
        for ymin, ymax, label, colour in OKTA_BANDS:
            ax.axhspan(ymin, ymax, color=colour, alpha=0.06, zorder=1)
            ax.text(
                1.002, (ymin + ymax) / 2, label,
                transform=ax.get_yaxis_transform(),
                va="center", ha="left", fontsize=7.5, color=colour, alpha=0.8,
            )

        # Step-filled area (zorder 5 / 6)
        ax.fill_between(times_ext, oktas_ext, step="post", color=CLOUD_FILL, alpha=0.28, zorder=5)
        ax.step(times_ext, oktas_ext, where="post", color=CLOUD_LINE, linewidth=1.8, zorder=6)

        # "Now" marker (zorder 7)
        now_local = datetime.now(timezone.utc).astimezone(HELSINKI_TZ)
        if times[0] <= now_local <= times[-1]:
            ax.axvline(now_local, color=ACCENT, linewidth=1.2, linestyle="--", alpha=0.8, zorder=7)
            ax.text(now_local, 8.15, "now", ha="center", va="bottom",
                    fontsize=8, color=ACCENT, alpha=0.9)

        # X-axis: midnight ticks show "Mon 17.03." (rotated 45°),
        # 06/12/18 ticks show just the two-digit hour "06", "12", "18".
        def _x_fmt(x, _pos):
            dt = mdates.num2date(x, tz=HELSINKI_TZ)
            return dt.strftime("%a %d.%m.") if dt.hour == 0 else dt.strftime("%H")

        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=HELSINKI_TZ))
        from matplotlib.ticker import FuncFormatter
        ax.xaxis.set_major_formatter(FuncFormatter(_x_fmt))
        # labelrotation=45 is persistent across redraws; ha='right' is applied
        # by the draw_event hook _on_after_draw so it survives zoom/pan too.
        ax.tick_params(axis="x", which="major", colors=SUBTEXT, labelsize=8.5,
                       length=4, labelrotation=45)
        ax.set_xlim(times_ext[0], times_ext[-1])
        self._xlim_full = ax.get_xlim()   # store as mpl floats for clamping
        self._pan_start = None

        # Recreate per-plot artists (ax.clear() destroys the old ones)
        self._annot     = self._make_annot()
        self._highlight = self._make_highlight()
        self._plot_times = times
        self._plot_oktas = oktas

        # Slider time marker (zorder 8) — initially hidden, positioned below
        self._slider_vline = ax.axvline(
            times[0], color="#ffd60a", linewidth=1.2, alpha=0.0, zorder=8,
        )
        self._update_slider_vline()

        self._canvas.draw_idle()

        valid = [o for o in oktas if o >= 0]
        avg = sum(valid) / len(valid) if valid else 0
        self._status_label.configure(
            text=f"5-day forecast  ·  avg {avg:.1f}/8 oktas  ·  {len(data)} steps"
        )

    # ── Zoom / pan ────────────────────────────────────────────────────────

    def _on_scroll(self, event):
        """Scroll wheel: zoom x-axis in/out centred on the cursor."""
        if event.inaxes is not self._ax or self._xlim_full is None:
            return
        xmin, xmax = self._ax.get_xlim()
        full_min, full_max = self._xlim_full
        factor = 0.72 if event.button == "up" else 1.39
        x_cur = event.xdata if event.xdata is not None else (xmin + xmax) / 2
        new_range = (xmax - xmin) * factor
        ratio = (x_cur - xmin) / (xmax - xmin)
        new_xmin = x_cur - ratio * new_range
        new_xmax = x_cur + (1 - ratio) * new_range
        # Clamp to data range
        if new_xmin < full_min:
            new_xmin, new_xmax = full_min, min(full_min + new_range, full_max)
        if new_xmax > full_max:
            new_xmin, new_xmax = max(full_max - new_range, full_min), full_max
        self._ax.set_xlim(new_xmin, new_xmax)
        self._canvas.draw_idle()

    def _on_button_press(self, event):
        if event.inaxes is not self._ax or self._xlim_full is None:
            return
        if event.button == 1:
            if event.dblclick:
                self._ax.set_xlim(*self._xlim_full)   # reset to full range
                self._canvas.draw_idle()
            else:
                self._pan_start = (event.xdata, self._ax.get_xlim())

    def _on_button_release(self, event):
        if event.button == 1:
            self._pan_start = None

    def _on_after_draw(self, _event):
        """After every draw, ensure date-tick labels keep ha='right' alignment.
        This makes the 45° rotation look correct even after zoom/pan redraws."""
        changed = False
        for lbl in self._ax.get_xticklabels():
            if " " in lbl.get_text() and lbl.get_ha() != "right":
                lbl.set_ha("right")
                changed = True
        if changed:
            self._canvas.draw_idle()

    def _show_error(self, message: str):
        self._status_label.configure(text="Error — see chart")
        ax = self._ax
        ax.clear()
        self._style_axes()
        ax.set_xticks([])
        ax.text(
            0.5, 0.5, message,
            transform=ax.transAxes, ha="center", va="center",
            color=DANGER, fontsize=11, linespacing=1.7,
        )
        self._annot = None
        self._highlight = None
        self._canvas.draw_idle()

    # ── Grid fetch & map overlay ───────────────────────────────────────────

    def _start_grid_fetch(self):
        threading.Thread(target=self._fetch_grid_bg, daemon=True).start()

    def _fetch_grid_bg(self):
        try:
            from src.fmi_grid import fetch_cloud_grid
            gf = fetch_cloud_grid(hours_ahead=120)
            self.after(0, self._on_grid_loaded, gf)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._on_grid_error, str(exc))

    def _on_grid_loaded(self, gf):
        self._grid_forecast = gf
        n = len(gf.times)
        # Find the index of the time step closest to "now"
        now_utc = datetime.now(timezone.utc)
        start_idx = min(range(n), key=lambda i: abs((gf.times[i] - now_utc).total_seconds()))

        self._time_slider.configure(
            to=n - 1,
            number_of_steps=n - 1,
            state="normal",
        )
        self._time_slider.set(start_idx)
        self._on_slider_change(start_idx)

    def _on_grid_error(self, msg: str):
        self._time_label.configure(text="Cloud grid unavailable")

    # ── Slider ────────────────────────────────────────────────────────────

    def _update_map_timestamp(self):
        """Draw/refresh the time label in the lower-right corner of the map canvas."""
        canvas = self._map.canvas
        for item_id in self._map_ts_ids:
            canvas.delete(item_id)
        self._map_ts_ids = []

        if self._slider_time is None:
            return

        dt = self._slider_time.astimezone(HELSINKI_TZ)
        text = dt.strftime("%a %d.%m.  %H:%M")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 10 or h < 10:
            return

        x, y = w - 12, h - 12
        font = ("Arial", 16, "bold")
        shadow = canvas.create_text(x + 1, y + 1, text=text, anchor="se",
                                    font=font, fill="#000000", tags="map_timestamp")
        label  = canvas.create_text(x,     y,     text=text, anchor="se",
                                    font=font, fill="#ffffff", tags="map_timestamp")
        self._map_ts_ids = [shadow, label]
        canvas.lift("map_timestamp")

    def _on_slider_change(self, value: float):
        gf = self._grid_forecast
        if gf is None:
            return
        idx = max(0, min(round(float(value)), len(gf.times) - 1))
        self._slider_time = gf.times[idx]
        dt = self._slider_time.astimezone(HELSINKI_TZ)
        self._time_label.configure(text=dt.strftime("%a %d.%m.  %H:%M"))
        self._update_slider_vline()
        self._update_map_timestamp()
        # Debounce: skip intermediate frames while the slider is dragged quickly
        if self._render_after_id is not None:
            self.after_cancel(self._render_after_id)
        self._render_after_id = self.after(40, self._render_overlay)

    def _update_slider_vline(self):
        """Move the chart's slider marker to the current _slider_time."""
        if self._slider_vline is None or self._plot_times is None:
            return
        if self._slider_time is None:
            self._slider_vline.set_alpha(0.0)
            self._canvas.draw_idle()
            return
        sl = self._slider_time.astimezone(HELSINKI_TZ)
        if self._plot_times[0] <= sl <= self._plot_times[-1]:
            self._slider_vline.set_xdata([sl])
            self._slider_vline.set_alpha(0.85)
        else:
            self._slider_vline.set_alpha(0.0)
        self._canvas.draw_idle()

    # ── Overlay rendering (fully vectorised — no Python loops) ────────────

    def _render_overlay(self):
        self._render_after_id = None

        gf = self._grid_forecast
        if gf is None or self._slider_time is None:
            return

        canvas_w = self._map.winfo_width()
        canvas_h = self._map.winfo_height()
        if canvas_w < 10 or canvas_h < 10:
            return

        # Nearest available time step
        target = self._slider_time
        t_idx = min(range(len(gf.times)),
                    key=lambda i: abs((gf.times[i] - target).total_seconds()))

        # ── Inverse Mercator: canvas pixel → lat / lon ────────────────────
        ul = self._map.upper_left_tile_pos
        lr = self._map.lower_right_tile_pos
        n = 2.0 ** round(self._map.zoom)

        cols = np.arange(canvas_w, dtype=np.float64)
        rows = np.arange(canvas_h, dtype=np.float64)

        tile_xs = ul[0] + (cols / canvas_w) * (lr[0] - ul[0])   # (canvas_w,)
        tile_ys = ul[1] + (rows / canvas_h) * (lr[1] - ul[1])   # (canvas_h,)

        pixel_lons = tile_xs / n * 360.0 - 180.0                        # (canvas_w,)
        pixel_lats = np.degrees(
            np.arctan(np.sinh(np.pi * (1.0 - 2.0 * tile_ys / n)))
        )                                                                 # (canvas_h,)

        # ── Nearest-neighbour lookup into the regular grid ────────────────
        lat_idx = np.clip(
            np.searchsorted(gf.lats, pixel_lats), 0, len(gf.lats) - 1,
        )   # (canvas_h,)
        lon_idx = np.clip(
            np.searchsorted(gf.lons, pixel_lons), 0, len(gf.lons) - 1,
        )   # (canvas_w,)

        # Broadcast to (canvas_h, canvas_w)
        oktas_px = gf.oktas[t_idx][lat_idx[:, np.newaxis], lon_idx[np.newaxis, :]]

        # ── Clip to Finland + surrounding waters ──────────────────────────
        # Mask pixels outside the geographic clip box as transparent
        CLIP_LAT_MIN, CLIP_LAT_MAX = 58.5, 71.5
        CLIP_LON_MIN, CLIP_LON_MAX = 18.0, 32.0
        outside = (
            (pixel_lats[:, np.newaxis] < CLIP_LAT_MIN) |
            (pixel_lats[:, np.newaxis] > CLIP_LAT_MAX) |
            (pixel_lons[np.newaxis, :] < CLIP_LON_MIN) |
            (pixel_lons[np.newaxis, :] > CLIP_LON_MAX)
        )
        oktas_px = oktas_px.copy()
        oktas_px[outside] = -1

        # ── Map oktas → RGBA via lookup table ─────────────────────────────
        rgba_table = np.array(_OKTA_RGBA, dtype=np.uint8)   # (9, 4)
        img_array = rgba_table[np.clip(oktas_px, 0, 8)]     # (canvas_h, canvas_w, 4)
        img_array[oktas_px < 0, 3] = 0                       # missing → transparent

        photo = PhotoImage(Image.fromarray(img_array, "RGBA"))
        canvas = self._map.canvas
        if self._overlay_canvas_id is not None:
            canvas.delete(self._overlay_canvas_id)
        self._overlay_canvas_id = canvas.create_image(
            0, 0, anchor="nw", image=photo, tags="cloud_overlay",
        )
        self._overlay_photo = photo   # prevent garbage collection

        # Z-order: above tiles, below markers / corner decorations
        canvas.lift("cloud_overlay")
        self._map.manage_z_order()

        self._overlay_map_state = (self._map.zoom, ul, lr)

    # ── Map movement / zoom ───────────────────────────────────────────────

    def _on_map_zoom_event(self, _event):
        """On scroll-wheel zoom: clear stale overlay immediately, re-render soon."""
        if self._overlay_canvas_id is not None:
            self._map.canvas.delete(self._overlay_canvas_id)
            self._overlay_canvas_id = None
            self._overlay_photo = None
        self._overlay_map_state = None   # force re-render on next poll

    def _poll_map_state(self):
        """Re-render the cloud overlay whenever the map pans or zooms."""
        if self._grid_forecast is not None and self._slider_time is not None:
            current = (
                self._map.zoom,
                self._map.upper_left_tile_pos,
                self._map.lower_right_tile_pos,
            )
            if current != self._overlay_map_state:
                self._render_overlay()
            elif self._overlay_canvas_id is not None:
                # Keep z-order correct as new tiles stream in
                self._map.canvas.lift("cloud_overlay")
                self._map.manage_z_order()
        if self._map_ts_ids:
            self._map.canvas.lift("map_timestamp")
        self.after(100, self._poll_map_state)
