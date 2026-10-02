# -*- coding: utf-8 -*-
"""
国内可用地图图层源生成器（基于反代）
=====================================
功能:
1. 自动探测可用反代主机（多候选健康检查，主反代失效自动切换）
2. 生成整套"可直接使用"的图源目录（谷歌卫星/混合/路网/地形 + 国产与开源备选）
3. 每个图源实测验证 + 探测当前区域实际可用的最高级别
4. 输出三种可直接使用的形态:
   - map_sources.json   图源库（含验证状态/最高级别/偏移警告）
   - qgis_xyz.ini       QGIS 可直接导入的连接片段
   - map_preview.html   双击即用的本地多图层预览页（Leaflet）

用法:
    python 地图图层源获取.py [纬度 经度]
"""
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
NOB = {"http": None, "https": None}

# ---------- 1. 反代候选池（按优先级，健康检查取第一个可用者） ----------
PROXY_CANDIDATES = [
    ("grc.io0.co",              "https://grc.io0.co/maps/vt?lyrs=s&x={x}&y={y}&z={z}"),
    ("mt1.google.com",          "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"),
    ("mt0.google.com",          "https://mt0.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"),
    ("gac-geo.googlecnapps.cn", "https://gac-geo.googlecnapps.cn/maps/vt?lyrs=s&x={x}&y={y}&z={z}"),
]

# ---------- 2. 图源目录 ----------
def build_catalog(proxy):
    """proxy: 可用反代主机。返回图源列表"""
    g = f"https://{proxy}/maps/vt"
    return [
        # name, url模板, 类型, 最大级别(探测上限), 备注
        ("谷歌卫星影像", f"{g}?lyrs=s&v=982&gl=cn&x={{x}}&y={{y}}&z={{z}}", "xyz", 21, "谷歌原始卫星瓦片，全球覆盖"),
        ("谷歌混合图", f"{g}?lyrs=y&v=982&gl=cn&x={{x}}&y={{y}}&z={{z}}", "xyz", 21, "卫星影像+路网地名标注"),
        ("谷歌路网图", f"{g}?lyrs=m&v=982&gl=cn&x={{x}}&y={{y}}&z={{z}}", "xyz", 21, "标准道路地图"),
        ("谷歌地形图", f"{g}?lyrs=p&v=982&gl=cn&x={{x}}&y={{y}}&z={{z}}", "xyz", 21, "带等高线阴影的地形图"),
        ("Esri卫星影像", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "xyz", 19, "免费无密钥，注意y在x前；中国乡村一般到18-19级"),
        ("Bing卫星影像", "https://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=14000", "quadkey", 19, "免费无密钥，{q}为四叉树键（QGIS不支持{q}，LSV/SAS Planet支持）"),
        ("高德卫星影像", "https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}", "xyz", 18, "⚠GCJ-02偏移约500m，叠WGS84数据会错位"),
        ("高德路网图", "https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}", "xyz", 18, "⚠GCJ-02偏移约500m"),
        ("OpenStreetMap", "https://a.tile.osm.org/{z}/{x}/{y}.png", "xyz", 19, "OSM标准图层（osm.org备用域名，openstreetmap.org主域被墙）"),
        ("ArcGIS Wayback 2024-11", "https://wayback-b.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/44710/{z}/{y}/{x}", "xyz", 19, "Esri历史影像快照"),
    ]


def quadkey(tx, ty, z):
    qk = ""
    for i in range(z, 0, -1):
        d = 0
        m = 1 << (i - 1)
        if tx & m:
            d += 1
        if ty & m:
            d += 2
        qk += str(d)
    return qk


def fetch_tile(url_tpl, lat, lon, z):
    """按模板取一张瓦片，返回 (状态码, 字节, 内容类型)"""
    n = 2.0 ** z
    tx = int((lon + 180.0) / 360.0 * n)
    ty = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    u = (url_tpl.replace("{x}", str(tx)).replace("{y}", str(ty))
         .replace("{z}", str(z)).replace("{q}", quadkey(tx, ty, z))
         .replace("{s}", "0"))
    try:
        r = requests.get(u, headers={"User-Agent": UA}, timeout=15, proxies=NOB)
        return r.status_code, r.content, r.headers.get("Content-Type", "")
    except Exception as e:
        return None, str(e).encode(), type(e).__name__


def is_real_image(content):
    return content[:2] in (b"\xff\xd8", b"\x89P") and len(content) > 100


def detect_proxy(lat, lon):
    """健康检查：返回第一个能出真实卫星瓦片的反代主机"""
    print("[1] 反代健康检查...")
    for host, tpl in PROXY_CANDIDATES:
        st, data, ct = fetch_tile(tpl, lat, lon, 17)
        ok = st == 200 and is_real_image(data) and len(data) > 3000
        print(f"    {host:26s} -> {'✓ 可用' if ok else '✗ 不可用'}"
              f"{'  ('+str(len(data))+'B)' if st == 200 else '  ('+str(st or '超时')+')'}")
        if ok:
            print(f"    => 采用反代: {host}\n")
            return host
    print("    => 所有候选均不可用！\n")
    return None


def validate_and_probe(sources, lat, lon, rural_lat, rural_lon):
    """每个源：两地点实测（市区+乡村）+ 卫星类源探测最高级别"""
    print("[3] 图源验证与最高级别探测...")
    # 市区点用长沙（保证路网/地形有数据）
    city = (28.228, 112.939)
    out = []
    for name, tpl, kind, zmax, note in sources:
        entry = {"name": name, "url": tpl, "type": kind,
                 "maxzoom_catalog": zmax, "note": note, "status": "?", "maxzoom": None}
        # 两点验证
        ok_pts = []
        for la, lo in ((city[0], city[1]), (rural_lat, rural_lon)):
            st, data, ct = fetch_tile(tpl, la, lo, 17)
            ok_pts.append(st == 200 and is_real_image(data))
        if any(ok_pts):
            entry["status"] = "OK"
        else:
            entry["status"] = "FAIL"
            out.append(entry)
            print(f"    ✗ {name:14s} 不可用")
            continue
        # 卫星/混合类探测实际最高级别
        if kind in ("xyz", "quadkey") and ("卫星" in name or "混合" in name or "Imagery" in name or "Wayback" in name):
            best = 16
            for z in range(17, zmax + 1):
                st, data, ct = fetch_tile(tpl, rural_lat, rural_lon, z)
                if st == 200 and is_real_image(data) and len(data) > 3000:
                    best = z
                else:
                    break
            entry["maxzoom"] = best
            print(f"    ✓ {name:14s} 最高级别 z{best}（本区域）")
        else:
            print(f"    ✓ {name:14s} 可用")
        out.append(entry)
    return out


def write_qgis_ini(sources, path):
    """生成 QGIS XYZ 连接片段（QGIS: 浏览器→XYZ→导入连接/或手动新建时对照填写）"""
    lines = ["[connections-xyz]"]
    for s in sources:
        if s["status"] != "OK" or s["type"] != "xyz":
            continue
        k = s["name"].replace(" ", "_")
        lines += [f"{k}\\url={s['url']}",
                  f"{k}\\zmax={s['maxzoom'] or s['maxzoom_catalog']}",
                  f"{k}\\zmin=0",
                  f"{k}\\authcfg=",
                  f"{k}\\username=",
                  f"{k}\\password=",
                  f"{k}\\referer=",
                  f"{k}\\tilePixelRatio=1"]
    open(path, "w", encoding="utf-8").write("\n".join(lines))


HTML_TPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>国内可用地图图层源预览</title>
<link rel="stylesheet" href="https://s4.zstatic.net/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://s4.zstatic.net/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
 html,body,#map{height:100%;margin:0}
 .coord{background:#fff;padding:2px 8px;font:12px monospace}
 .warn{color:#c00;font-size:11px}
 .leaflet-control-layers{font-size:13px}
</style>
</head>
<body>
<div id="map"></div>
<script>
const SOURCES = __SOURCES__;
const AOI = __AOI__;
const map = L.map('map',{zoomControl:true}).setView([AOI.lat,AOI.lon], AOI.zoom);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'OSM'}).addTo(map);
const base = {};
let first = true;
for (const s of SOURCES) {
  if (s.status !== 'OK') continue;
  const opt = {maxZoom: s.maxzoom || s.maxzoom_catalog, attribution: s.name};
  let layer;
  if (s.type === 'quadkey') {
    // Leaflet 不支持 {q}，做一层转换
    layer = L.tileLayer('', opt);
    layer.getTileUrl = function(c) {
      let q=''; const n=1<<c.z;
      for (let i=c.z;i>0;i--){let d=0;const m=1<<(i-1);
        if(c.x&m)d+=1; if(c.y&m)d+=2; q+=d;}
      return s.url.replace('{q}',q);
    };
  } else {
    layer = L.tileLayer(s.url, opt);
  }
  base[s.name + (s.note&&s.note.includes('偏移')?' ⚠偏移':'')] = layer;
  if (first && s.name.includes('卫星')) { layer.addTo(map); first = false; }
}
L.control.layers(base, null, {collapsed:false}).addTo(map);
L.rectangle([[AOI.lat1,AOI.lon1],[AOI.lat2,AOI.lon2]],
  {color:'#ff3333',weight:2,fill:false}).addTo(map);
L.control.scale({metric:true}).addTo(map);
const coord = L.control({position:'bottomleft'});
coord.onAdd = function(){
  const div = L.DomUtil.create('div','coord');
  map.on('mousemove', e => div.textContent = e.latlng.lat.toFixed(6)+' , '+e.latlng.lng.toFixed(6));
  return div;
};
coord.addTo(map);
</script>
</body>
</html>
"""


def main():
    # 用户矩形中心 & 范围
    lat1, lon1 = 29.36178, 113.70284
    lat2, lon2 = 29.36964, 113.70472
    lat = (lat1 + lat2) / 2
    lon = (lon1 + lon2) / 2
    if len(sys.argv) >= 3:
        lat, lon = float(sys.argv[1]), float(sys.argv[2])

    print("=" * 66)
    print("国内可用地图图层源生成器")
    print(f"验证中心: ({lat}, {lon})  矩形AOI: ({lat1},{lon1})~({lat2},{lon2})")
    print("=" * 66)

    proxy = detect_proxy(lat, lon)
    if not proxy:
        print("无可用反代，仅生成非谷歌图源")
        proxy = "invalid.proxy"
    google_ok = proxy not in ("mt1.google.com",)  # mt1 直连能通也算
    sources = build_catalog(proxy)
    if proxy == "invalid.proxy":
        sources = [s for s in sources if "谷歌" not in s[0]]

    print(f"[2] 已构建 {len(sources)} 个图源目录")
    results = validate_and_probe(sources, lat, lon, lat, lon)

    # 输出 JSON
    catalog = {
        "proxy_used": proxy,
        "test_point": {"lat": lat, "lon": lon},
        "sources": results,
    }
    json.dump(catalog, open("map_sources.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    write_qgis_ini(results, "qgis_xyz.ini")

    # 输出 HTML 预览
    ok_sources = [s for s in results if s["status"] == "OK"]
    html = (HTML_TPL
            .replace("__SOURCES__", json.dumps(ok_sources, ensure_ascii=False))
            .replace("__AOI__", json.dumps(
                {"lat": lat, "lon": lon, "zoom": 15,
                 "lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2},
                ensure_ascii=False)))
    open("map_preview.html", "w", encoding="utf-8").write(html)

    print("\n[4] 输出完成:")
    n_ok = sum(1 for s in results if s["status"] == "OK")
    print(f"    map_sources.json  — {len(results)} 个图源（{n_ok} 个可用）")
    print(f"    qgis_xyz.ini      — QGIS XYZ 连接片段（可导入）")
    print(f"    map_preview.html  — 双击打开的多图层预览页")
    print("\n    QGIS 导入方法: 浏览器面板 → XYZ Tiles → 右键'导入连接'")
    print("    或新建连接时对照 qgis_xyz.ini 内的 URL 填写")
    print("=" * 66)


if __name__ == "__main__":
    main()
