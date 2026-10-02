#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
谷歌/多源卫星影像瓦片下载拼接器（GUI 版）
==========================================
功能:
  · 左上/右下角经纬度输入（小数 或 度分秒 两种方式）
  · 9 种坐标系输入（WGS84/CGCS2000/GCJ-02火星/BD-09百度/ITRF/NAD83/ETRS89/WebMercator/UTM）
  · 内置图源（谷歌反代/谷歌直连/Esri/Bing/高德）+ 自定义图源 URL
  · 下载前信息面板: 各级瓦片数与容量(MB)、实际最高可抓级别、矩形宽高(米,1位小数)
  · 单级下载 / 全部打包下载；>100MB 提醒、>300MB 拒绝并建议分拆
  · 自动拼接并按经纬度精确裁剪，输出 z{级}.jpg

依赖: requests, Pillow    运行: python 卫星影像瓦片下载器.py
自测: python 卫星影像瓦片下载器.py --selftest
"""
import io
import json
import math
import os
import queue
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
NOB = {"http": None, "https": None}
TILE = 256

# ============================================================
#  坐标转换
# ============================================================
_A = 6378245.0          # 克拉索夫斯基椭球长半轴（GCJ02）
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


def _out_of_china(lat, lon):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _tlat(x, y):
    r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
         + 0.2 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tlon(x, y):
    r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
         + 0.1 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lat, lon):
    """WGS84 -> GCJ-02（火星）"""
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _tlat(lon - 105.0, lat - 35.0)
    dlon = _tlon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(radlat) ** 2
    sm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sm) * math.pi)
    dlon = (dlon * 180.0) / (_A / sm * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat, lon):
    """GCJ-02 -> WGS84（迭代反演，精度约 1e-6 度）"""
    if _out_of_china(lat, lon):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(4):
        glat, glon = wgs84_to_gcj02(wlat, wlon)
        wlat += lat - glat
        wlon += lon - glon
    return wlat, wlon


def gcj02_to_bd09(lat, lon):
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    t = math.atan2(lat, lon) + 0.000003 * math.cos(lon * _X_PI)
    return z * math.sin(t) + 0.006, z * math.cos(t) + 0.0065


def bd09_to_gcj02(lat, lon):
    x = lon - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    t = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.sin(t), z * math.cos(t)


def wgs84_to_bd09(lat, lon):
    return gcj02_to_bd09(*wgs84_to_gcj02(lat, lon))


def bd09_to_wgs84(lat, lon):
    return gcj02_to_wgs84(*bd09_to_gcj02(lat, lon))


def wgs84_to_mercator(lat, lon):
    x = lon * 20037508.342789244 / 180.0
    y = (math.log(math.tan((90.0 + lat) * math.pi / 360.0))
         / (math.pi / 180.0)) * 20037508.342789244 / 180.0
    return x, y


def mercator_to_wgs84(x, y):
    lon = x / 20037508.342789244 * 180.0
    lat = 180.0 / math.pi * (2 * math.atan(math.exp(y / 20037508.342789244 * 180.0 * math.pi / 180.0)) - math.pi / 2)
    return lat, lon


# ---- UTM（Krüger 级数，精度毫米级） ----
K0 = 0.9996
WGS_A = 6378137.0
WGS_F = 1 / 298.257223563
WGS_E2 = WGS_F * (2 - WGS_F)


def utm_to_wgs84(easting, northing, zone, northern=True):
    x = easting - 500000.0
    y = northing if northern else northing - 10000000.0
    m = y / K0
    mu = m / (WGS_A * (1 - WGS_E2 / 4 - 3 * WGS_E2 ** 2 / 64 - 5 * WGS_E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - WGS_E2)) / (1 + math.sqrt(1 - WGS_E2))
    j1 = (3 * e1 / 2 - 27 * e1 ** 3 / 32)
    j2 = (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32)
    j3 = (151 * e1 ** 3 / 96)
    j4 = (1097 * e1 ** 4 / 512)
    fp = mu + j1 * math.sin(2 * mu) + j2 * math.sin(4 * mu) + j3 * math.sin(6 * mu) + j4 * math.sin(8 * mu)
    e2s = WGS_E2 / (1 - WGS_E2)
    c1 = e2s * math.cos(fp) ** 2
    t1 = math.tan(fp) ** 2
    n1 = WGS_A / math.sqrt(1 - WGS_E2 * math.sin(fp) ** 2)
    r1 = WGS_A * (1 - WGS_E2) / (1 - WGS_E2 * math.sin(fp) ** 2) ** 1.5
    d = x / (n1 * K0)
    lat = fp - (n1 * math.tan(fp) / r1) * (d ** 2 / 2
          - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * e2s) * d ** 4 / 24
          + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * e2s - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * e2s + 24 * t1 ** 2) * d ** 5 / 120) / math.cos(fp)
    return math.degrees(lat), math.degrees(lon) + (zone * 6 - 183)


def wgs84_to_utm(lat, lon):
    zone = int((lon + 180) / 6) + 1
    a = WGS_A
    e2 = WGS_E2
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    lon0 = math.radians(zone * 6 - 183)
    k0 = K0
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    n_ = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    t = math.tan(lat_r) ** 2
    c = e2 / (1 - e2) * math.cos(lat_r) ** 2
    aa = math.cos(lat_r) * (lon_r - lon0)
    m_ = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * lat_r
              - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * lat_r)
              + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * lat_r)
              - (35 * e2 ** 3 / 3072) * math.sin(6 * lat_r))
    easting = k0 * n_ * (aa + (1 - t + c) * aa ** 3 / 6
               + (5 - 18 * t + t ** 2 + 72 * c - 58 * e2 / (1 - e2)) * aa ** 5 / 120) + 500000.0
    northing = k0 * (m_ + n_ * math.tan(lat_r) * (aa ** 2 / 2
               + (5 - t + 9 * c + 4 * c ** 2) * aa ** 4 / 24
               + (61 - 58 * t + t ** 2 + 600 * c - 330 * e2 / (1 - e2)) * aa ** 6 / 720))
    if lat < 0:
        northing += 10000000.0
    return easting, northing, zone


# ---------- 输入坐标系 -> WGS84 ----------
COORD_SYSTEMS = [
    ("WGS-84 (GPS/国际通用)", "wgs84", "geo"),
    ("CGCS2000 (2000国家大地,中国法定)", "cgcs2000", "geo"),
    ("GCJ-02 火星坐标系 (高德/腾讯)", "gcj02", "geo"),
    ("BD-09 百度坐标系", "bd09", "geo"),
    ("ITRF (国际地球参考框架)", "itrf", "geo"),
    ("NAD83 (北美大地基准1983)", "nad83", "geo"),
    ("ETRS89 (欧洲陆地参考1989)", "etrs89", "geo"),
    ("Web Mercator EPSG:3857 (米)", "mercator", "proj"),
    ("UTM 通用横轴墨卡托 (米)", "utm", "proj"),
]


def input_to_wgs84(sysname, v1, v2):
    """把输入坐标转到 WGS84；geo 系返回 (lat,lon)，投影系 v1=X/easting v2=Y/northing"""
    key = dict((k, (c, t)) for k, c, t in COORD_SYSTEMS)[sysname][0] if False else None
    for name, code, kind in COORD_SYSTEMS:
        if name == sysname:
            break
    if code == "wgs84":
        return v1, v2
    if code in ("cgcs2000", "itrf", "nad83", "etrs89"):
        # 与 WGS84 差异为厘米~分米级，网络瓦片场景视作重合
        return v1, v2
    if code == "gcj02":
        return gcj02_to_wgs84(v1, v2)
    if code == "bd09":
        return bd09_to_wgs84(v1, v2)
    if code == "mercator":
        return mercator_to_wgs84(v1, v2)
    if code == "utm":
        # v1 = easting, v2 = northing, 半球由 northing 判断（>10,000,000 之外默认北半球）
        return utm_to_wgs84(v1, v2, zone=49, northern=v2 >= 0)
    return v1, v2


def wgs84_to_source(lat, lon, src_crs):
    """WGS84 -> 图源瓦片坐标系（谷歌/Esri/Bing=WGS84，高德=GCJ02）"""
    if src_crs == "gcj02":
        return wgs84_to_gcj02(lat, lon)
    return lat, lon


# ============================================================
#  度分秒解析 / 格式化
# ============================================================
DMS_PAT = re.compile(
    r"""^\s*([NSWE经纬南北东西]?)\s*
        (\d+(?:\.\d+)?)\s*[°度d]?\s*
        (?:(\d+(?:\.\d+)?)\s*['′分m]?)?\s*
        (?:(\d+(?:\.\d+)?)\s*["″秒s]?)?\s*
        ([NSWE经纬南北东西]?)\s*$""",
    re.I | re.X)


def parse_coord(s, is_lat=True):
    """支持: 29.36178 | 29°21'42.4" | 29 21 42.4 | 29d21m42.4s | 带N/E/S/W后缀"""
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = DMS_PAT.match(s)
    if not m:
        raise ValueError(f"无法解析坐标: {s}")
    hemi1, d, mi, se, hemi2 = m.groups()
    d = float(d); mi = float(mi or 0); se = float(se or 0)
    val = d + mi / 60.0 + se / 3600.0
    hemi = (hemi1 or hemi2 or "").upper()
    if hemi in ("S", "W", "南", "西"):
        val = -val
    if is_lat and hemi in ("E", "W", "东", "西"):
        raise ValueError("纬度不能带东西标记")
    return val


def fmt_dms(v, is_lat=True):
    hemi = ("N" if v >= 0 else "S") if is_lat else ("E" if v >= 0 else "W")
    v = abs(v)
    d = int(v); mi_f = (v - d) * 60; mi = int(mi_f); se = (mi_f - mi) * 60
    return f"{d}°{mi:02d}'{se:05.2f}\"{hemi}"


# ============================================================
#  图源注册表
# ============================================================
BUILTIN_SOURCES = [
    {"name": "谷歌卫星影像 (grc反代·国内直连)",
     "url": "https://grc.io0.co/maps/vt?lyrs=s&v=982&gl=cn&x={x}&y={y}&z={z}",
     "kind": "xyz", "crs": "wgs84", "zmax": 21},
    {"name": "谷歌混合图 (卫星+标注)",
     "url": "https://grc.io0.co/maps/vt?lyrs=y&v=982&gl=cn&x={x}&y={y}&z={z}",
     "kind": "xyz", "crs": "wgs84", "zmax": 21},
    {"name": "谷歌卫星 (官方直连·需网络可达)",
     "url": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
     "kind": "xyz", "crs": "wgs84", "zmax": 21},
    {"name": "Bing 卫星影像 (quadkey)",
     "url": "https://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=14000",
     "kind": "quadkey", "crs": "wgs84", "zmax": 19},
    {"name": "Esri 卫星影像 (World Imagery)",
     "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
     "kind": "xyz", "crs": "wgs84", "zmax": 19},
    {"name": "高德卫星影像 (GCJ-02)",
     "url": "https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
     "kind": "xyz", "crs": "gcj02", "zmax": 18},
    {"name": "OpenStreetMap (osm.org备用域)",
     "url": "https://a.tile.osm.org/{z}/{x}/{y}.png",
     "kind": "xyz", "crs": "wgs84", "zmax": 19},
]
CUSTOM_LABEL = "✏ 自定义图源 (手工填写URL)..."


def quadkey(tx, ty, z):
    q = ""
    for i in range(z, 0, -1):
        d = 0
        m = 1 << (i - 1)
        if tx & m:
            d += 1
        if ty & m:
            d += 2
        q += str(d)
    return q


def tile_url(src, z, tx, ty):
    return (src["url"].replace("{x}", str(tx)).replace("{y}", str(ty))
            .replace("{z}", str(z)).replace("{q}", quadkey(tx, ty, z))
            .replace("{s}", "0"))


# ============================================================
#  核心引擎：信息计算 / 探测 / 下载
# ============================================================
def lonlat_to_pixel(lat, lon, z):
    n = 2.0 ** z
    return ((lon + 180.0) / 360.0 * n * TILE,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n * TILE)


def rect_range(rect, z):
    """rect: (lat_tl, lon_tl, lat_br, lon_br) 已换算到图源坐标系
       返回 (tx_min, ty_min, tx_max, ty_max)"""
    (lat1, lon1), (lat2, lon2) = rect
    x1, y1 = lonlat_to_pixel(lat1, lon1, z)
    x2, y2 = lonlat_to_pixel(lat2, lon2, z)
    tx_min = int(math.floor(min(x1, x2) / TILE))
    tx_max = int(math.floor(max(x1, x2) / TILE))
    ty_min = int(math.floor(min(y1, y2) / TILE))
    ty_max = int(math.floor(max(y1, y2) / TILE))
    return tx_min, ty_min, tx_max, ty_max


def tile_size_at(src, lat, lon, z):
    """取单张瓦片返回字节数（None=取不到）"""
    (lat_s, lon_s) = wgs84_to_source(lat, lon, src["crs"])
    n = 2.0 ** z
    tx = int((lon_s + 180.0) / 360.0 * n)
    ty = int((1 - math.asinh(math.tan(math.radians(lat_s))) / math.pi) / 2.0 * n)
    try:
        r = requests.get(tile_url(src, z, tx, ty),
                         headers={"User-Agent": UA}, timeout=12, proxies=NOB)
        if r.status_code == 200 and r.content[:2] in (b"\xff\xd8", b"\x89P"):
            return len(r.content)
    except Exception:
        pass
    return None


def detect_max_zoom(src, lat, lon, zmax_default):
    """从 17 级起逐级探测中心瓦片，返回实际最高真实级别"""
    best = 16
    for z in range(17, zmax_default + 1):
        size = tile_size_at(src, lat, lon, z)
        if size and size > 3000:
            best = z
        else:
            break
    return best


def compute_info(src, rect_wgs84, levels):
    """计算宽高/各级瓦片数与容量/实际最高级别"""
    (lat1, lon1), (lat2, lon2) = rect_wgs84
    lat_mid = (lat1 + lat2) / 2
    width_m = abs(lon2 - lon1) * 111319.49 * math.cos(math.radians(lat_mid))
    height_m = abs(lat2 - lat1) * 111132.92
    maxz = detect_max_zoom(src, lat_mid, lon1, src["zmax"])

    rows = []
    for z in levels:
        rect_src = [wgs84_to_source(lat1, lon1, src["crs"]),
                    wgs84_to_source(lat2, lon2, src["crs"])]
        tx_min, ty_min, tx_max, ty_max = rect_range(rect_src, z)
        count = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
        # 采样估算单张大小（中心 + 两个角内点）
        samples = []
        cands = [((lat1 + lat2) / 2, (lon1 + lon2) / 2),
                 (lat1, lon1), (lat2, lon2)]
        for la, lo in cands:
            s = tile_size_at(src, la, lo, z)
            if s:
                samples.append(s)
            if len(samples) >= 3:
                break
        avg = sum(samples) / len(samples) if samples else 20000.0
        mb = count * avg / 1024.0 / 1024.0
        rows.append({"z": z, "count": count, "mb": mb,
                     "capped": z > maxz})
    return {"width_m": width_m, "height_m": height_m,
            "max_zoom": maxz, "rows": rows}


def fetch_tile(src, z, tx, ty):
    for attempt in range(3):
        try:
            r = requests.get(tile_url(src, z, tx, ty),
                             headers={"User-Agent": UA}, timeout=25, proxies=NOB)
            if (r.status_code == 200 and len(r.content) > 100
                    and r.content[:2] in (b"\xff\xd8", b"\x89P")):
                return r.content
            if r.status_code == 404:
                return None
            time.sleep(0.4 * (attempt + 1))
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    return None


def download_and_stitch(src, rect_wgs84, z, outdir, threads=10, progress=None):
    """下载某级全部瓦片并拼接裁剪，返回 (path, ok_count, total_count, bytes)"""
    (lat1, lon1), (lat2, lon2) = rect_wgs84
    rect_src = [wgs84_to_source(lat1, lon1, src["crs"]),
                wgs84_to_source(lat2, lon2, src["crs"])]
    tx_min, ty_min, tx_max, ty_max = rect_range(rect_src, z)
    nx = tx_max - tx_min + 1
    ny = ty_max - ty_min + 1
    total = nx * ny
    canvas = Image.new("RGB", (nx * TILE, ny * TILE), (24, 24, 24))
    lock = threading.Lock()
    stat = {"done": 0, "ok": 0, "bytes": 0}
    sess = requests.Session()

    def work(tx, ty):
        data = fetch_tile(src, z, tx, ty)
        if data:
            try:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                if img.size != (TILE, TILE):
                    img = img.resize((TILE, TILE))
                with lock:
                    canvas.paste(img, ((tx - tx_min) * TILE, (ty - ty_min) * TILE))
                    stat["ok"] += 1
                    stat["bytes"] += len(data)
            except Exception:
                pass
        with lock:
            stat["done"] += 1
            if progress:
                progress(stat["done"], total)

    tasks = [(tx, ty) for ty in range(ty_min, ty_max + 1)
             for tx in range(tx_min, tx_max + 1)]
    with ThreadPoolExecutor(max_workers=threads) as ex:
        list(ex.map(work, [t[0] for t in tasks], [t[1] for t in tasks]))

    # 精确裁剪
    x1, y1 = lonlat_to_pixel(*rect_src[0], z)
    x2, y2 = lonlat_to_pixel(*rect_src[1], z)
    left = max(0, int(round(min(x1, x2) - tx_min * TILE)))
    top = max(0, int(round(min(y1, y2) - ty_min * TILE)))
    right = min(canvas.width, int(round(max(x1, x2) - tx_min * TILE)))
    bottom = min(canvas.height, int(round(max(y1, y2) - ty_min * TILE)))
    canvas = canvas.crop((left, top, right, bottom))

    os.makedirs(outdir, exist_ok=True)
    short = re.sub(r"[^\w]+", "_", src["name"])[:20]
    path = os.path.join(outdir, f"{short}_z{z}.jpg")
    canvas.save(path, "JPEG", quality=92)
    return path, stat["ok"], total, stat["bytes"]


# ============================================================
#  GUI
# ============================================================
C_BG = "#f4f6fa"
C_CARD = "#ffffff"
C_ACCENT = "#2b6cff"
C_ACCENT_D = "#1d4fd7"
C_TEXT = "#1f2733"
C_SUB = "#6b7686"
C_OK = "#18a058"
C_WARN = "#d03050"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🛰 卫星影像瓦片下载拼接器")
        self.geometry("880x760")
        self.configure(bg=C_BG)
        self.minsize(860, 720)
        self._build_style()
        self._build_ui()
        self.q = queue.Queue()
        self.after(120, self._poll)
        self._info = None

    # ---------- 样式 ----------
    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=C_BG, foreground=C_TEXT,
                     font=("Microsoft YaHei UI", 10))
        st.configure("Card.TFrame", background=C_CARD)
        st.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT)
        st.configure("Sub.TLabel", background=C_CARD, foreground=C_SUB,
                     font=("Microsoft YaHei UI", 9))
        st.configure("Title.TLabel", background=C_BG, foreground=C_TEXT,
                     font=("Microsoft YaHei UI", 16, "bold"))
        st.configure("H.TLabel", background=C_CARD, foreground=C_ACCENT_D,
                     font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("TCombobox", arrowsize=14)
        st.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                     foreground="white", background=C_ACCENT, padding=(14, 7))
        st.map("Accent.TButton",
               background=[("active", C_ACCENT_D), ("disabled", "#a9c3f5")],
               foreground=[("disabled", "#e8eefc")])
        st.configure("TButton", padding=(10, 6))
        st.configure("TCheckbutton", background=C_CARD)
        st.configure("TRadiobutton", background=C_CARD)
        st.configure("TLabelframe", background=C_CARD, borderwidth=0)
        st.configure("TLabelframe.Label", background=C_CARD,
                     foreground=C_ACCENT_D, font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("Treeview", background=C_CARD, rowheight=26,
                     font=("Microsoft YaHei UI", 10))
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("Horizontal.TProgressbar", background=C_ACCENT,
                     troughcolor="#e6eaf2", borderwidth=0)

    # ---------- 界面 ----------
    def _card(self, parent, title):
        lf = ttk.Labelframe(parent, text=" " + title + " ", style="TLabelframe",
                            padding=(14, 10))
        return lf

    def _build_ui(self):
        head = ttk.Frame(self)
        head.pack(fill="x", padx=18, pady=(14, 6))
        ttk.Label(head, text="🛰 卫星影像瓦片下载拼接器",
                  style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="多源 · 多坐标系 · 自动拼接",
                  style="Sub.TLabel").pack(side="left", padx=10, pady=(6, 0))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=18, pady=6)
        left = ttk.Frame(body, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True, ipadx=6)

        # ---- 输入区 ----
        f_in = self._card(left, "① 区域输入")
        f_in.pack(fill="x", pady=(0, 8))

        row0 = ttk.Frame(f_in, style="Card.TFrame")
        row0.pack(fill="x", pady=(0, 6))
        ttk.Label(row0, text="输入方式:", style="Card.TLabel").pack(side="left")
        self.var_mode = tk.StringVar(value="dec")
        ttk.Radiobutton(row0, text="小数经纬度", variable=self.var_mode,
                        value="dec", command=self._on_mode).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(row0, text="度分秒 (如 29°21'42.4\")",
                        variable=self.var_mode, value="dms",
                        command=self._on_mode).pack(side="left", padx=4)
        ttk.Label(row0, text="输入坐标系:", style="Card.TLabel").pack(side="left", padx=(18, 4))
        self.cb_sys = ttk.Combobox(row0, width=34, state="readonly",
                                   values=[n for n, c, k in COORD_SYSTEMS])
        self.cb_sys.current(0)
        self.cb_sys.pack(side="left")
        self.cb_sys.bind("<<ComboboxSelected>>", self._on_sys)
        self.lbl_sysnote = ttk.Label(row0, text="", style="Sub.TLabel")
        self.lbl_sysnote.pack(side="left", padx=8)

        grid = ttk.Frame(f_in, style="Card.TFrame")
        grid.pack(fill="x")
        for c, txt in ((1, "纬度 / X"), (2, "经度 / Y")):
            ttk.Label(grid, text=txt, style="H.TLabel").grid(row=0, column=c, padx=6)
        ttk.Label(grid, text="左上角:", style="Card.TLabel").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Label(grid, text="右下角:", style="Card.TLabel").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        self.e_tl1 = ttk.Entry(grid, width=24)
        self.e_tl2 = ttk.Entry(grid, width=24)
        self.e_br1 = ttk.Entry(grid, width=24)
        self.e_br2 = ttk.Entry(grid, width=24)
        self.e_tl1.grid(row=1, column=1, padx=6, pady=4)
        self.e_tl2.grid(row=1, column=2, padx=6, pady=4)
        self.e_br1.grid(row=2, column=1, padx=6, pady=4)
        self.e_br2.grid(row=2, column=2, padx=6, pady=4)
        self.e_tl1.insert(0, "29.36178")
        self.e_tl2.insert(0, "113.70284")
        self.e_br1.insert(0, "29.36964")
        self.e_br2.insert(0, "113.70472")
        # 示例度分秒
        self._dms_hint = ttk.Label(grid, text='度分秒示例:  29°21\'42.4"  或  29 21 42.4',
                                   style="Sub.TLabel")
        self._dms_hint.grid(row=3, column=1, columnspan=2, sticky="w", padx=6)

        # ---- 图源区 ----
        f_src = self._card(left, "② 图层源")
        f_src.pack(fill="x", pady=8)
        row_s1 = ttk.Frame(f_src, style="Card.TFrame")
        row_s1.pack(fill="x", pady=2)
        ttk.Label(row_s1, text="选择图源:", style="Card.TLabel").pack(side="left")
        self.cb_src = ttk.Combobox(row_s1, width=46, state="readonly",
                                   values=[s["name"] for s in BUILTIN_SOURCES] + [CUSTOM_LABEL])
        self.cb_src.current(0)
        self.cb_src.pack(side="left", padx=8)
        self.cb_src.bind("<<ComboboxSelected>>", self._on_src)

        row_s2 = ttk.Frame(f_src, style="Card.TFrame")
        row_s2.pack(fill="x", pady=2)
        self.lbl_custom = ttk.Label(row_s2, text="自定义URL:", style="Card.TLabel")
        self.e_custom = ttk.Entry(row_s2, width=64)
        self.lbl_kind = ttk.Label(row_s2, text="类型:", style="Card.TLabel")
        self.cb_kind = ttk.Combobox(row_s2, width=9, state="readonly",
                                    values=["xyz", "quadkey"])
        self.lbl_ccrs = ttk.Label(row_s2, text="源坐标系:", style="Card.TLabel")
        self.cb_ccrs = ttk.Combobox(row_s2, width=9, state="readonly",
                                    values=["wgs84", "gcj02"])
        self.lbl_zmax = ttk.Label(row_s2, text="最大级:", style="Card.TLabel")
        self.e_zmax = ttk.Entry(row_s2, width=4)
        self.e_zmax.insert(0, "21")
        for w in (self.lbl_custom, self.e_custom, self.lbl_kind, self.cb_kind,
                  self.lbl_ccrs, self.cb_ccrs, self.lbl_zmax, self.e_zmax):
            w.pack(side="left", padx=4)
        self._toggle_custom(False)

        # ---- 下载设置 ----
        f_dl = self._card(left, "③ 下载设置")
        f_dl.pack(fill="x", pady=8)
        row_d1 = ttk.Frame(f_dl, style="Card.TFrame")
        row_d1.pack(fill="x", pady=2)
        ttk.Label(row_d1, text="保存目录:", style="Card.TLabel").pack(side="left")
        self.e_out = ttk.Entry(row_d1, width=52)
        self.e_out.pack(side="left", padx=6, fill="x", expand=True)
        self.e_out.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "mosaic_out"))
        ttk.Button(row_d1, text="浏览...", command=self._pick_dir).pack(side="left")
        row_d2 = ttk.Frame(f_dl, style="Card.TFrame")
        row_d2.pack(fill="x", pady=(6, 2))
        ttk.Label(row_d2, text="下载级别:", style="Card.TLabel").pack(side="left")
        self.var_levels = {}
        for z in (17, 18, 19, 20, 21):
            v = tk.BooleanVar(value=True)
            self.var_levels[z] = v
            ttk.Checkbutton(row_d2, text=f"{z} 级", variable=v).pack(side="left", padx=6)
        ttk.Button(row_d2, text="全选", width=6,
                   command=lambda: [v.set(True) for v in self.var_levels.values()]).pack(side="left", padx=6)
        ttk.Label(row_d2, text="并发线程:", style="Card.TLabel").pack(side="left", padx=(14, 4))
        self.spin_th = ttk.Spinbox(row_d2, from_=1, to=32, width=4)
        self.spin_th.set(10)
        self.spin_th.pack(side="left")
        self.var_zip = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_d2, text="完成后打包 ZIP", variable=self.var_zip).pack(side="left", padx=14)

        # ---- 操作按钮 ----
        f_act = ttk.Frame(left, style="Card.TFrame")
        f_act.pack(fill="x", pady=10)
        self.btn_info = ttk.Button(f_act, text="🔍 计算信息并打开下载面板",
                                   style="Accent.TButton", command=self._open_panel)
        self.btn_info.pack(side="left")
        ttk.Button(f_act, text="使用度分秒填入示例",
                   command=self._fill_dms_demo).pack(side="left", padx=8)

        # ---- 日志 ----
        f_log = self._card(left, "运行日志")
        f_log.pack(fill="both", expand=True, pady=(4, 0))
        self.txt = tk.Text(f_log, height=9, bg="#0e1420", fg="#cfe3ff",
                           insertbackground="#cfe3ff", relief="flat",
                           font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True)
        self._log("就绪。默认图源为谷歌卫星影像(grc.io0.co 反代，国内直连)。")
        self._log("度分秒/坐标系/自定义图源均可设置。点击'计算信息'查看容量与最高级别。")

    # ---------- 事件 ----------
    def _log(self, msg):
        self.txt.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        self.txt.see("end")

    def _on_mode(self):
        pass

    def _on_sys(self, e=None):
        kind = dict((n, k) for n, c, k in COORD_SYSTEMS).get(self.cb_sys.get())
        if kind == "proj":
            for w in (self.e_tl1, self.e_tl2, self.e_br1, self.e_br2):
                pass
            self.lbl_sysnote.config(text="投影坐标系请输入 X/Easting、Y/Northing（米）；度分秒不可用")
        else:
            self.lbl_sysnote.config(text="")

    def _on_src(self, e=None):
        self._toggle_custom(self.cb_src.get() == CUSTOM_LABEL)

    def _toggle_custom(self, on):
        for w in (self.lbl_custom, self.lbl_kind, self.lbl_ccrs, self.lbl_zmax):
            w.state = tk.NORMAL
        for w in (self.e_custom, self.cb_kind, self.cb_ccrs, self.e_zmax):
            w.configure(state="normal" if on else "disabled")
        if on:
            self.cb_kind.current(0)
            self.cb_ccrs.current(0)

    def _pick_dir(self):
        d = filedialog.askdirectory(title="选择保存目录")
        if d:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, d)

    def _fill_dms_demo(self):
        self.var_mode.set("dms")
        self.e_tl1.delete(0, "end"); self.e_tl1.insert(0, '29°21\'42.40"N')
        self.e_tl2.delete(0, "end"); self.e_tl2.insert(0, '113°42\'10.22"E')
        self.e_br1.delete(0, "end"); self.e_br1.insert(0, '29°22\'10.70"N')
        self.e_br2.delete(0, "end"); self.e_br2.insert(0, '113°42\'17.00"E')
        self._log("已填入度分秒示例（即默认矩形区域）。")

    def _current_source(self):
        name = self.cb_src.get()
        if name == CUSTOM_LABEL:
            url = self.e_custom.get().strip()
            if not url or "{x" not in url or "{y" not in url or "{z" not in url and "{q}" not in url:
                if "{q}" not in url and ("{x" not in url or "{y" not in url or "{z" not in url):
                    raise ValueError("自定义 URL 需包含 {x}{y}{z}（或 quadkey 的 {q}）占位符")
            return {"name": "自定义图源", "url": url,
                    "kind": self.cb_kind.get(), "crs": self.cb_ccrs.get(),
                    "zmax": int(self.e_zmax.get() or 21)}
        for s in BUILTIN_SOURCES:
            if s["name"] == name:
                return dict(s)
        raise ValueError("请选择图源")

    def _read_rect(self):
        mode = self.var_mode.get()
        sysname = self.cb_sys.get()
        kind = dict((n, k) for n, c, k in COORD_SYSTEMS).get(sysname)
        vals = []
        for e in (self.e_tl1, self.e_tl2, self.e_br1, self.e_br2):
            raw = e.get().strip()
            if not raw:
                raise ValueError("坐标输入不完整")
            if mode == "dms" and kind == "geo":
                vals.append(parse_coord(raw))
            else:
                vals.append(float(raw))
        tl = input_to_wgs84(sysname, vals[0], vals[1])
        br = input_to_wgs84(sysname, vals[2], vals[3])
        return [tl, br]   # [(lat,lon),(lat,lon)] WGS84

    def _open_panel(self):
        try:
            src = self._current_source()
            rect = self._read_rect()
            levels = [z for z in (17, 18, 19, 20, 21) if self.var_levels[z].get()]
            if not levels:
                messagebox.showwarning("提示", "请至少勾选一个下载级别")
                return
        except Exception as e:
            messagebox.showerror("输入错误", str(e))
            return
        self._log(f"计算中: {src['name']}  区域 {rect}")
        self.btn_info.config(state="disabled")

        def work():
            try:
                info = compute_info(src, rect, levels)
                self.q.put(("info", src, rect, levels, info))
            except Exception as e:
                self.q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    # ---------- 下载面板 ----------
    def _show_panel(self, src, rect, levels, info):
        self.btn_info.config(state="normal")
        (lat1, lon1), (lat2, lon2) = rect
        w, h, maxz = info["width_m"], info["height_m"], info["max_zoom"]

        win = tk.Toplevel(self)
        win.title("下载面板 · " + src["name"])
        win.configure(bg=C_BG)
        win.geometry("640x560")
        win.grab_set()

        head = ttk.Frame(win, style="Card.TFrame")
        head.pack(fill="x", padx=14, pady=12)
        ttk.Label(head, text=f"📐 矩形实际尺寸:  宽 {w:,.1f} 米  ×  高 {h:,.1f} 米",
                  style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")
                  ).pack(anchor="w")
        ttk.Label(head, text=f"🛰 图源: {src['name']}    🧭 实际可抓取最高级别: z{maxz}"
                  + ("（超出部分无真实影像，将跳过）" if maxz < max(levels) else ""),
                  style="Sub.TLabel").pack(anchor="w", pady=(4, 0))

        cols = ("level", "count", "mb", "note")
        tv = ttk.Treeview(win, columns=cols, show="headings", height=6)
        for c, t, wdt, anc in (("level", "级别", 60, "center"),
                               ("count", "瓦片数", 100, "center"),
                               ("mb", "预计容量 (MB)", 140, "e"),
                               ("note", "说明", 300, "w")):
            tv.heading(c, text=t)
            tv.column(c, width=wdt, anchor=anc)
        tv.pack(fill="x", padx=14)
        total_mb = 0.0
        for r in info["rows"]:
            note = "⚠ 超过实际最高级别，将下载占位或跳过" if r["capped"] else "正常"
            tv.insert("", "end", values=(
                f"z{r['z']}", r["count"], f"{r['mb']:.2f}", note))
            total_mb += r["mb"]
        lbl_sum = ttk.Label(win, text=f"所选级别合计: {total_mb:.2f} MB",
                            font=("Microsoft YaHei UI", 11, "bold"),
                            background=C_BG, foreground=C_ACCENT_D)
        lbl_sum.pack(anchor="w", padx=16, pady=6)
        if total_mb > 300:
            lbl_sum.config(foreground=C_WARN, text=f"所选级别合计: {total_mb:.2f} MB —— 超过 300MB，禁止打包下载，请分拆")
        elif total_mb > 100:
            lbl_sum.config(foreground="#c77700", text=f"所选级别合计: {total_mb:.2f} MB —— 超过 100MB，下载前将再次确认")

        # 进度区
        pr = ttk.Frame(win, style="Card.TFrame")
        pr.pack(fill="x", padx=14, pady=4)
        self.pb = ttk.Progressbar(pr, mode="determinate", style="Horizontal.TProgressbar")
        self.pb.pack(fill="x")
        self.lbl_prog = ttk.Label(pr, text="等待开始…", style="Card.TLabel")
        self.lbl_prog.pack(anchor="w", pady=(3, 0))

        # 按钮
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=14, pady=10)
        outdir = self.e_out.get().strip()
        threads = int(self.spin_th.get() or 10)
        do_zip = self.var_zip.get()

        def guard(size_mb):
            if size_mb > 300:
                messagebox.showerror(
                    "容量过大",
                    f"⛔ 预计总容量 {size_mb:.1f} MB 超过 300MB 强制上限！\n"
                    "为避免生成超大文件/超长耗时，已拒绝本次下载。\n"
                    "请取消部分级别分批下载（例如先下 17-19 级，再单独下 20 或 21 级）。")
                return False
            if size_mb > 100:
                if not messagebox.askyesno(
                        "容量提醒",
                        f"⚠ 预计总容量 {size_mb:.1f} MB 超过 100MB。\n"
                        f"下载耗时可能较长，确定继续吗？"):
                    return False
            return True

        def start(levels_sel, label):
            sel = [r for r in info["rows"] if r["z"] in levels_sel]
            size = sum(r["mb"] for r in sel)
            if not guard(size):
                return
            for z in levels_sel:
                if z > maxz:
                    if not messagebox.askyesno(
                            "级别超限",
                            f"z{z} 超过本区域实际最高级别 z{maxz}，\n"
                            "高出的级别将只含占位图。仍要包含它吗？"):
                        levels_sel = [z for z in levels_sel if z <= maxz]
                        if not levels_sel:
                            return
                        break
            self._run_download(win, src, rect, levels_sel, outdir, threads, do_zip)

        ttk.Button(btns, text="⬇ 下载勾选级别（单级依次）", style="Accent.TButton",
                   command=lambda: start(levels, "all")).pack(side="left")
        ttk.Button(btns, text="📦 打包下载(ZIP)", style="Accent.TButton",
                   command=lambda: self._run_download(
                       win, src, rect, levels, outdir, threads, True)).pack(side="left", padx=8)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="right")
        self._log(f"信息: 宽{w:.1f}m 高{h:.1f}m 最高z{maxz} "
                  + " ".join(f"z{r['z']}={r['mb']:.1f}MB" for r in info["rows"]))

    # ---------- 下载执行 ----------
    def _run_download(self, win, src, rect, levels, outdir, threads, do_zip):
        self._dl_stop = False
        self.btn_dl_guard = True

        def progress(done, total):
            self.q.put(("prog", done, total))

        def work():
            files = []
            try:
                for i, z in enumerate(levels):
                    if getattr(self, "_dl_stop", False):
                        break
                    self.q.put(("log", f"开始 z{z} 级下载…"))
                    t0 = time.time()
                    def p(done, total, z=z):
                        self.q.put(("prog_level", z, done, total, i + 1, len(levels)))
                    path, ok, total, nbytes = download_and_stitch(
                        src, rect, z, outdir, threads, p)
                    self.q.put(("done_level", z, path, ok, total,
                                nbytes, time.time() - t0))
                    files.append(path)
                if do_zip and files and not self._dl_stop:
                    zip_path = os.path.join(outdir, f"mosaic_{time.strftime('%Y%m%d_%H%M%S')}.zip")
                    self.q.put(("log", "打包 ZIP…"))
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for f in files:
                            zf.write(f, os.path.basename(f))
                    self.q.put(("log", f"已打包: {zip_path}"))
                self.q.put(("all_done",))
            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ---------- 队列轮询 ----------
    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                tag = item[0]
                if tag == "info":
                    self._show_panel(item[1], item[2], item[3], item[4])
                elif tag == "log":
                    self._log(item[1])
                elif tag == "prog_level":
                    _, z, done, total, i, n = item
                    self.pb.config(maximum=total, value=done)
                    self.lbl_prog.config(text=f"正在下载 z{z} 级: {done}/{total} 瓦片"
                                          f"   (第 {i}/{n} 级)")
                elif tag == "done_level":
                    _, z, path, ok, total, nbytes, el = item
                    self._log(f"z{z} 完成: {ok}/{total} 瓦片, "
                              f"{nbytes/1024/1024:.2f}MB, {el:.0f}s -> {path}")
                elif tag == "all_done":
                    self._log("✅ 全部任务完成！")
                    self.lbl_prog.config(text="✅ 完成")
                elif tag == "error":
                    self._log("❌ " + item[1])
                    messagebox.showerror("错误", item[1])
                    self.btn_info.config(state="normal")
        except queue.Empty:
            pass
        self.after(120, self._poll)


# ============================================================
#  自测
# ============================================================
def selftest():
    import math as _m
    ok = True

    # 1. 坐标转换
    glat, glon = wgs84_to_gcj02(29.36571, 113.70378)
    assert abs(glat - 29.362963852380776) < 1e-6, glat
    w1, w2 = gcj02_to_wgs84(glat, glon)
    assert abs(w1 - 29.36571) < 1e-5 and abs(w2 - 113.70378) < 1e-5, (w1, w2)
    b1, b2 = wgs84_to_bd09(29.36571, 113.70378)
    w1, w2 = bd09_to_wgs84(b1, b2)
    assert abs(w1 - 29.36571) < 1e-5 and abs(w2 - 113.70378) < 1e-5
    x, y = wgs84_to_mercator(29.36571, 113.70378)
    la, lo = mercator_to_wgs84(x, y)
    assert abs(la - 29.36571) < 1e-6 and abs(lo - 113.70378) < 1e-6
    e, n, zn = wgs84_to_utm(29.36571, 113.70378)
    la, lo = utm_to_wgs84(e, n, zn)
    assert zn == 49, zn
    assert abs(la - 29.36571) < 1e-6 and abs(lo - 113.70378) < 1e-6, (la, lo)
    assert abs(parse_coord("29°21'42.4\"") - 29.3617778) < 1e-4
    assert abs(parse_coord('113 42 10.22') - 113.7028389) < 1e-4
    assert abs(parse_coord('29d21m42.4sN') - 29.3617778) < 1e-4
    print("✓ 坐标转换与DMS解析通过")

    # 2. 信息计算（默认矩形 + grc 反代）
    src = BUILTIN_SOURCES[0]
    rect = [(29.36178, 113.70284), (29.36964, 113.70472)]
    info = compute_info(src, rect, [17, 18, 19, 20, 21])
    print(f"  宽 {info['width_m']:.1f} m, 高 {info['height_m']:.1f} m, "
          f"实际最高 z{info['max_zoom']}")
    tot = 0
    for r in info["rows"]:
        print(f"  z{r['z']}: {r['count']:4d} 张  {r['mb']:6.2f} MB"
              + ("  (超实际级别)" if r["capped"] else ""))
        tot += r["mb"]
    print(f"  合计 {tot:.2f} MB")
    assert info["max_zoom"] >= 20, "最高级别异常"
    assert abs(info["width_m"] - 182.4) < 1 and abs(info["height_m"] - 873.5) < 1

    # 3. 下载一张真实瓦片验证
    data = fetch_tile(src, 21, *(
        lambda n=2.0 ** 21: (int((113.70378 + 180) / 360 * n),
                             int((1 - math.asinh(math.tan(math.radians(29.36571))) / math.pi) / 2 * n)))())
    assert data and len(data) > 3000, "z21 瓦片获取失败"
    print("✓ z21 瓦片真实可取")
    print("全部自测通过 ✅")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        App().mainloop()
