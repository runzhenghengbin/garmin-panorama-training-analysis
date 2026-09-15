#!/usr/bin/env python3
"""
Garmin 全景训练分析 —— 报告生成脚本
=====================================
读取 fetch_data.py 产出的 JSON，生成 Ride Relief 暗色风格的单文件交互式 HTML 报告。

用法：
    python3 scripts/generate_report.py                        # 用 data/ 下最新的 JSON
    python3 scripts/generate_report.py data/garmin_x_y.json   # 指定输入
    python3 scripts/generate_report.py --focus-run 639036629  # 指定焦点跑（逐公里分析对象）

特性：
  - Chart.js 内联（离线可打开，无需联网）
  - 内置双重质量门禁：JS 语法检查（Node）+ 图表数据完整性自检，FAIL 直接退出非 0
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "references"))

import heat_metrics as hm  # noqa: E402
import load_balance as lb  # noqa: E402
import body_composition as bc  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
CSS_FILE = SKILL_ROOT / "references" / "ride_relief_style.css"
CHARTJS_FILE = SKILL_ROOT / "assets" / "chart.umd.min.js"

CHART_COLORS = {
    "accent": "#d6ff64", "cool": "#72e8ff", "warn": "#ffc857",
    "danger": "#ff6b5f", "muted": "#8b938a", "ink": "#f1f3e9",
    "grid": "rgba(235,240,225,0.08)", "bg": "#0b0d0c",
}


def fmt_pace(s):
    return lb.fmt_pace(s)


def fmt_dur(s):
    if not s:
        return "—"
    s = int(s)
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def md_label(d):
    """2026-09-13 → 09/13（⚠️ 顺序不可颠倒：月/日）"""
    return f"{int(d[5:7]):02d}/{int(d[8:10]):02d}"


# --------------------------------------------------------------------------- #
# 数据加工
# --------------------------------------------------------------------------- #
def build_daily_stats(data):
    """按日期聚合同日多次跑步（同日多跑必须合并，否则堆叠图 x 轴出现重复标签）。"""
    runs = [a for a in data["activities"] if a["category"] == "run" and a["distance_km"] > 0.2]
    start = data["meta"]["start"]
    end = data["meta"]["end"]
    days = []
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    d = d0
    while d <= d1:
        ds = d.isoformat()
        same = [r for r in runs if r["date"] == ds]
        dist = round(sum(r["distance_km"] for r in same), 2)
        days.append({
            "date": ds,
            "label": md_label(ds),
            "dist": dist,
            "count": len(same),
            "load": round(sum(r.get("training_stress_score") or 0 for r in same), 1),
            "hr": round(sum(r["avg_hr"] for r in same if r.get("avg_hr")) / len([r for r in same if r.get("avg_hr")])) if any(r.get("avg_hr") for r in same) else None,
        })
        d += timedelta(days=1)
    return days, runs


def build_weeks(runs):
    """按自然周（周一为起点）聚合跑量。"""
    wk = {}
    for r in runs:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        monday = d - timedelta(days=d.weekday())
        wk.setdefault(monday, 0.0)
        wk[monday] += r["distance_km"]
    out = []
    for m in sorted(wk):
        out.append({
            "label": f"{m.strftime('%m/%d')}周",
            "dist": round(wk[m], 2),
        })
    return out


def aggregate_zones(runs):
    """汇总所有跑步的心率分区秒数（Garmin 5 区），返回 (seconds[], bounds[])。"""
    zone_seconds = {}
    bounds = {}
    for r in runs:
        for z in r.get("hr_zones") or []:
            n = z.get("zone")
            if not n:
                continue
            zone_seconds[n] = zone_seconds.get(n, 0) + (z.get("seconds") or 0)
            if z.get("low"):
                bounds[n] = z["low"]
    if not zone_seconds:
        return [], []
    zones = sorted(zone_seconds)
    seconds = [round(zone_seconds[z]) for z in zones]
    bl = [bounds.get(z) for z in zones]
    return seconds, bl


def evaluate_heat(runs, lt_hr):
    """逐次跑步的热应激评价（含 WBGT / 体感 / 跨日适应信号）。"""
    if lt_hr:
        hm.LT_HR = float(lt_hr)
    rows = []
    for r in runs:
        w = r.get("weather") or {}
        t = w.get("temp_c")
        rh = w.get("humidity")
        if t is None or rh is None:
            continue
        pace = r.get("moving_pace_s")
        hr = r.get("avg_hr")
        if not pace or not hr:
            continue
        rows.append(hm.evaluate_run(
            T=float(t), RH=float(rh), wind_kmh=float(w.get("wind_kmh") or 0),
            avg_hr=float(hr), pace_s=float(pace),
            label=r["date"][5:], note=r.get("name") or "",
        ))
    rows.sort(key=lambda x: x["label"])
    return rows, hm.acclimation_table(rows)


def build_recovery(data):
    """逐日恢复序列：HRV / RHR / 睡眠 / Body Battery / 压力。"""
    daily = data.get("daily") or []
    out = []
    for d in daily:
        out.append({
            "label": md_label(d["date"]),
            "hrv": d.get("hrv"),
            "rhr": d.get("resting_hr"),
            "sleep_h": d.get("sleep_h"),
            "sleep_score": d.get("sleep_score"),
            "bb": d.get("body_battery_high"),
            "stress": d.get("stress_avg"),
        })
    return out


def compute_readiness(data):
    """用最近一天的数据 + 7 天基线算就绪度。"""
    daily = [d for d in (data.get("daily") or [])]
    if not daily:
        return None
    last = daily[-1]
    hist = daily[-8:-1] or daily
    hrvs = [d["hrv"] for d in hist if d.get("hrv")]
    rhrs = [d["resting_hr"] for d in hist if d.get("resting_hr")]
    ts = data.get("training_status") or {}
    score, parts = lb.readiness(
        sleep_score=last.get("sleep_score"),
        hrv=last.get("hrv"),
        hrv_base=(sum(hrvs) / len(hrvs)) if hrvs else None,
        rhr=last.get("resting_hr"),
        rhr_base=(sum(rhrs) / len(rhrs)) if rhrs else None,
        bb_high=last.get("body_battery_high"),
        stress_avg=last.get("stress_avg"),
        acute=ts.get("acute_load"),
        chronic=ts.get("chronic_load"),
    )
    return {"score": score, "label": lb.readiness_label(score), "parts": parts,
            "hrv_base": round(sum(hrvs) / len(hrvs), 1) if hrvs else None,
            "rhr_base": round(sum(rhrs) / len(rhrs), 1) if rhrs else None}


def build_energy(data):
    """身体成分与能量（LBM / BMR / TDEE / 跑量消耗 / 宏量目标）。"""
    cfg = data["config"]
    ath = data["athlete"]
    w = ath.get("weight_kg")
    bf = ath.get("bodyfat_pct")
    if not w:
        return None
    lbm = bc.lbm(w, bf) if bf else None
    bmr = bc.bmr_katch(lbm) if lbm else bc.bmr_mifflin(
        w, cfg["athlete"].get("height_cm") or 170, cfg["athlete"].get("age") or 35,
        (cfg["athlete"].get("sex", "male") == "male"))
    tdee = bc.tdee(bmr, cfg.get("activity_factor", 1.55))
    runs = [a for a in data["activities"] if a["category"] == "run"]
    total_km = sum(a["distance_km"] for a in runs)
    days = max(data["meta"].get("days") or 1, 1)
    run_kcal = bc.running_kcal(w, total_km)
    wk = total_km / days * 7
    m = cfg.get("macro") or {}
    macro = bc.macro_targets(w, m.get("protein_g_per_kg", 2.0), m.get("carb_g_per_kg", 5.0),
                             m.get("fat_g_per_kg", 1.0))
    return {
        "weight": w, "bodyfat": bf, "lbm": round(lbm, 1) if lbm else None,
        "bmr": round(bmr), "tdee": round(tdee), "total_km": round(total_km, 1),
        "weekly_km": round(wk, 1), "run_kcal": round(run_kcal),
        "run_kcal_per_day": round(run_kcal / days),
        "macro": macro,
    }


def build_prediction(data, runs):
    """Riegel 成绩预测 + 目标配速对比 + 倒计时。"""
    goals = (data.get("config") or {}).get("goals") or {}
    samples = [(r["distance_km"], r["moving_s"]) for r in runs if r.get("moving_s") and r["distance_km"] >= 3]
    if not samples:
        return None
    target_km = goals.get("race_distance_km") or 42.195
    pred = lb.predict_race(samples, target_km)
    if not pred:
        return None
    out = {"pred": pred, "goal": goals, "gap_s": None, "days_left": None,
           "garmin_pred": None}

    tf = goals.get("target_finish")
    if tf:
        try:
            parts = [int(x) for x in tf.split(":")]
            target_s = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
            out["target_s"] = target_s
            out["gap_s"] = round(pred["predicted_s"] - target_s)
            out["target_pace_s"] = round(target_s / target_km, 1)
        except Exception:  # noqa: BLE001
            pass

    # 佳明官方成绩预测（Garmin Connect 自带，与 Riegel 并列展示互为校验）
    # 必须放在 target_s 赋值之后 —— 下面要用它算 gap
    ath = data.get("athlete") or {}
    gsec = ath.get("race_prediction_sec") or {}
    dist_map = {"5K": 5.0, "10K": 10.0, "半马": 21.0975, "全马": 42.195}
    best_k, best_d = None, None
    for lbl, sec in gsec.items():
        dk = dist_map.get(lbl)
        if not dk or not sec:
            continue
        diff = abs(dk - target_km)
        if best_d is None or diff < best_d:
            best_k, best_d = lbl, diff
    if best_k:
        gs = gsec[best_k]
        out["garmin_pred"] = {
            "label": best_k, "sec": gs,
            "time": (ath.get("race_predictions") or {}).get(best_k),
            "pace_s": round(gs / dist_map[best_k], 1),
            "delta_s": round(gs - pred["predicted_s"]),
        }
        if out.get("target_s"):
            out["garmin_pred"]["gap_s"] = round(gs - out["target_s"])

    rd = goals.get("race_date")
    if rd:
        try:
            out["days_left"] = (datetime.strptime(rd, "%Y-%m-%d").date() - date.today()).days
        except Exception:  # noqa: BLE001
            pass
    return out


def build_recommendations(data, ctx):
    """规则化教练建议（每条都基于真实数据，不写空话）。"""
    recs = []
    ts = data.get("training_status") or {}
    acwr_v = ts.get("acwr")
    if acwr_v is not None:
        key, name, advice = lb.classify_acwr(acwr_v)
        recs.append((
            "负荷平衡", f"ACWR {acwr_v}：{name}",
            f"急性负荷 {ts.get('acute_load') or '—'} / 慢性负荷 {ts.get('chronic_load') or '—'}，"
            f"Form {lb.form(ts.get('chronic_load'), ts.get('acute_load'))}。{advice}。"
            f"Garmin 负荷平衡提示：{lb.load_balance_zh((ts.get('load_balance') or {}).get('trainingBalanceFeedbackPhrase'))}。",
            "cool" if key == "balanced" else ("warn" if key in ("detraining", "rising") else "danger"),
        ))
    rd = ctx["readiness"]
    if rd and rd["score"] is not None:
        recs.append((
            "今日就绪度", f"{rd['score']} 分 · {rd['label']}",
            "构成：" + "、".join(f"{k} {s}分" for k, (_w, s) in rd["parts"].items()) +
            f"。HRV 基线 {rd['hrv_base'] or '—'} ms、静息心率基线 {rd['rhr_base'] or '—'} bpm。",
            "cool" if rd["score"] >= 65 else "warn",
        ))
    zd = ctx["zone_dist"]
    if zd:
        top_i = zd.index(max(zd))
        recs.append((
            "强度分布", f"主力区间 Z{top_i + 1} 占 {zd[top_i]}%",
            "低强度（Z1-Z2）占比 " + f"{sum(zd[:2])}%" + "，中高强度（Z3+）" + f"{sum(zd[2:])}%。"
            + ("有氧基础扎实，继续保持 80/20 比例。" if sum(zd[:2]) >= 50
               else "低强度有氧偏少，建议把轻松跑真正跑轻松（能完整说话），为 10 月全马打底。"),
            "cool" if sum(zd[:2]) >= 40 else "warn",
        ))
    heat = ctx["heat_rows"]
    if heat:
        hottest = max(heat, key=lambda r: r["wbgt"])
        grade, _c, advice = hottest["grade"], hottest["color"], hottest["advice"]
        recs.append((
            "热适应", f"最热一次 {hottest['label']}：WBGT {hottest['wbgt']:.1f}°C（{grade}）",
            f"气温 {hottest['T']}°C / 湿度 {hottest['RH']}% / 风 {hottest['wind']}km/h，"
            f"体感 {hottest['apparent']:.1f}°C。{advice}。"
            + ("注意：高湿下鞋袜湿透是物理必然，不是退步信号，看心率与配速效率即可。"
               if hottest["saturated"] else "仍有余热的季节建议日出前后跑，同样配速心率更低。"),
            "warn" if hottest["wbgt"] >= 23 else "cool",
        ))
    pred = ctx["prediction"]
    if pred and pred.get("gap_s") is not None:
        gap = pred["gap_s"]
        recs.append((
            "目标差距", f"预测 {pred['pred']['predicted_txt']} vs 目标 {pred['goal'].get('target_finish')}",
            (f"按 Riegel 由 {pred['pred']['from_km']}km 推算，比目标快 {abs(gap)//60} 分，"
             f"保持当前负荷并守住 {pred['pred']['pace_txt']}/km 即可。" if gap < 0 else
             f"按 Riegel 由 {pred['pred']['from_km']}km 推算，距目标还差 {gap//60} 分 "
             f"（目标配速 {fmt_pace(pred.get('target_pace_s'))}/km）；长距离样本越多预测越准，"
             f"建议每周一次 26–32km 的 LSD 打底。"),
            "cool" if gap < 0 else "warn",
        ))
    tech = ctx["tech_stability"]
    if tech:
        recs.append((
            "跑姿经济性", f"焦点跑步频 {tech['cad_mean']:.0f} spm · 步幅 {tech['stride_mean']:.2f} m · 垂直振幅 {tech['vo_mean']:.1f} cm",
            f"垂直比均值 {tech['vr_mean']:.1f}%（越低越经济），触地 {tech['gct_mean']:.0f} ms。"
            + ("末段步幅衰减 " + f"{tech['stride_drop']:.1f}%" + "，属正常疲劳范围。"
               if tech["stride_drop"] < 8 else f"末段步幅衰减 {tech['stride_drop']:.1f}%，超出正常范围（<8%），提示长距离耐力或补给需加强。"),
            "cool" if tech["stride_drop"] < 8 else "warn",
        ))
    wk = ctx["weeks"]
    if len(wk) >= 2:
        delta = wk[-1]["dist"] - wk[-2]["dist"]
        target = (data["config"].get("goals") or {}).get("weekly_volume_target_km")
        recs.append((
            "周跑量节奏", f"最近一周 {wk[-1]['dist']:.1f} km（环比 {delta:+.1f} km）",
            (f"目标周跑量 {target} km，" if target else "")
            + ("增量控制在 10% 以内更安全。" if delta > 0 and wk[-2]["dist"] and delta / wk[-2]["dist"] > 0.3
               else "增量温和，处于可持续区间。"),
            "cool" if delta <= 0 or not wk[-2]["dist"] or delta / wk[-2]["dist"] <= 0.3 else "warn",
        ))
    return recs


# --------------------------------------------------------------------------- #
# HTML 渲染
# --------------------------------------------------------------------------- #
def render_html(data, ctx):
    css = CSS_FILE.read_text(encoding="utf-8")
    chartjs = ""
    cdn_fallback = ""
    if CHARTJS_FILE.exists():
        chartjs = CHARTJS_FILE.read_text(encoding="utf-8")
    else:
        cdn_fallback = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>'

    meta = data["meta"]
    ath = data["athlete"]
    ov = ctx["overview"]
    goals = (data.get("config") or {}).get("goals") or {}
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # ---------- JS 数据（全部预先 json.dumps，禁止在 f-string 里再写 f-string） ----------
    js = {k: json.dumps(v, ensure_ascii=False) for k, v in ctx["js"].items()}

    # ---------- 组件 ----------
    weather_badge = ""
    if ctx["focus_weather"]:
        w = ctx["focus_weather"]
        weather_badge = (
            f'<div class="weather-badge">🌡 <b>{w.get("temp_c")}°C</b> · 💧 <b>{w.get("humidity")}%</b>'
            f' · 💨 <b>{w.get("wind_kmh")} km/h</b> · ☁ <b>{esc(w.get("desc") or "—")}</b>'
            + (f' · ⌚ 手表实测 <b>{w.get("device_temp_c")}°C</b>' if w.get("device_temp_c") else '')
            + f' <span class="tag cool">{esc(w.get("source"))}</span></div>'
        )

    zone_rows = ""
    if ctx["zone_seconds"] and ctx["zone_bounds"]:
        total_s = sum(ctx["zone_seconds"]) or 1
        for i, s in enumerate(ctx["zone_seconds"]):
            pct = s / total_s * 100
            lo = ctx["zone_bounds"][i]
            hi = ctx["zone_bounds"][i + 1] if i + 1 < len(ctx["zone_bounds"]) else None
            rng = f"{lo}–{hi}" if hi else f"{lo}+"
            zone_rows += (
                f"<tr><td>Z{i+1}</td><td>{rng}</td><td>{s/60:.0f} min</td>"
                f"<td><b>{pct:.1f}%</b></td></tr>"
            )

    heat_cards = ""
    if ctx["heat_rows"]:
        h = ctx["heat_rows"][-1]
        heat_cards = f"""
      <div class="heat-metrics">
        <div class="heat-card"><span>气温</span><strong>{h['T']}°C</strong><small>干球温度</small></div>
        <div class="heat-card"><span>湿度</span><strong>{h['RH']}%</strong><small>相对湿度</small></div>
        <div class="heat-card"><span>风速</span><strong>{h['wind']} km/h</strong><small>蒸发散热</small></div>
        <div class="heat-card"><span>澳式体感</span><strong>{h['apparent']:.1f}°C</strong><small>Steadman AT</small></div>
        <div class="heat-card"><span>酷热指数</span><strong>{h['heat_index']:.1f}°C</strong><small>NOAA HI</small></div>
        <div class="heat-card accent"><span>估算 WBGT</span><strong>{h['wbgt']:.1f}°C</strong><small>{h['grade']}</small></div>
      </div>"""

    heat_table = ""
    if ctx["heat_rows"]:
        for i, r in enumerate(ctx["heat_rows"]):
            hl = " class='hl'" if i == len(ctx["heat_rows"]) - 1 else ""
            heat_table += (
                f"<tr{hl}><td>{r['label']}</td><td>{r['T']}°C</td><td>{r['RH']}%</td>"
                f"<td>{r['wind']}</td><td><b>{r['wbgt']:.1f}</b></td><td>{r['apparent']:.1f}°C</td>"
                f"<td>{fmt_pace(r['pace_s'])}</td><td>{r['avg_hr']}</td><td>{r['pct_lt']:.0f}%</td>"
                f"<td>{r.get('heat_penalty', 0):+.0f}s</td></tr>"
            )

    heat_signals = "".join(f"<li>{esc(s)}</li>" for s in (ctx["heat_signals"] or []))
    if not heat_signals:
        heat_signals = "<li>暂无跨日同配速样本，继续累积热季数据</li>"

    km_rows = ""
    for s in ctx["focus_splits"]:
        km_rows += (
            f"<tr><td>{s['km']}</td><td>{s['pace']}</td><td>{s['avg_hr'] or '—'}</td>"
            f"<td>{s['cadence'] or '—'}</td><td>{s['stride_m'] or '—'}</td>"
            f"<td>{s['vertical_osc_cm'] or '—'}</td><td>{s['vertical_ratio'] or '—'}</td>"
            f"<td>{s['gct_ms'] or '—'}</td><td>{s['avg_power'] or '—'}</td></tr>"
        )

    recs_html = ""
    for i, (cat, title, body, tone) in enumerate(ctx["recommendations"], 1):
        recs_html += f"""
        <div class="recommendation-card">
          <div class="recommendation-number">{i}</div>
          <div>
            <span class="recommendation-meta">{esc(cat)}</span>
            <h3>{esc(title)}</h3>
            <p><span class="tag {tone}">{tone}</span> {esc(body)}</p>
          </div>
        </div>"""

    energy = ctx["energy"]
    energy_html = ""
    if energy:
        mc = energy["macro"]
        energy_html = f"""
      <div class="metric-grid">
        <div><span>体重</span><strong>{energy['weight']} kg</strong><small>体脂 {energy['bodyfat'] or '—'}%</small></div>
        <div><span>去脂体重 LBM</span><strong class="accent-val">{energy['lbm'] or '—'} kg</strong><small>Katch-McArdle 基准</small></div>
        <div><span>基础代谢 BMR</span><strong>{energy['bmr']} kcal</strong><small>静息消耗/天</small></div>
        <div><span>总消耗 TDEE</span><strong>{energy['tdee']} kcal</strong><small>活动因子 {data['config'].get('activity_factor')}</small></div>
        <div><span>区间跑量</span><strong>{energy['total_km']} km</strong><small>折合周 {energy['weekly_km']} km</small></div>
        <div><span>跑步消耗</span><strong>{energy['run_kcal']} kcal</strong><small>日均 {energy['run_kcal_per_day']} kcal</small></div>
        <div><span>蛋白目标</span><strong>{mc['protein_g']} g</strong><small>{mc['protein_kcal']} kcal · {mc['pct_protein']}%</small></div>
        <div><span>碳水目标</span><strong class="accent-val">{mc['carb_g']} g</strong><small>{mc['carb_kcal']} kcal · {mc['pct_carb']}%</small></div>
      </div>
      <div class="analysis-card"><span class="kicker">ENERGY</span>
        <strong>每日总消耗 ≈ {energy['tdee'] + energy['run_kcal_per_day']} kcal（含跑步）</strong>
        <p>按 {energy['weight']}kg 体重、周跑量 {energy['weekly_km']}km 估算。减脂期建议缺口 300–500 kcal/天；
           全马备赛期（赛前 3 周起）碳水补足到 <b>{mc['carb_g']}g/天</b>，长跑日按每小时 40–60g 补给。</p>
      </div>"""

    pred_html = ""
    pred = ctx["prediction"]
    if pred:
        p = pred["pred"]
        gap_txt = "—"
        if pred.get("gap_s") is not None:
            g = pred["gap_s"]
            gap_txt = f"快 {abs(g)//60} 分 {abs(g)%60} 秒" if g < 0 else f"慢 {g//60} 分 {g%60} 秒"
        # 佳明官方预测卡片：必须在这里先算好（禁止在外层 f-string 内再嵌 f-string）
        garmin_card = ""
        gp = pred.get("garmin_pred")
        if gp:
            d = gp["delta_s"]
            cmp_txt = (f"比 Riegel 快 {abs(d) // 60} 分" if d < 0 else f"比 Riegel 慢 {abs(d) // 60} 分")
            gap2 = gp.get("gap_s")
            gap2_txt = ""
            if gap2 is not None:
                gap2_txt = f"，{'快' if gap2 < 0 else '慢'}目标 {abs(gap2) // 60} 分"
            garmin_card = f"""
      <div class="analysis-card"><span class="kicker">GARMIN OFFICIAL</span>
        <strong>佳明官方预测{gp['label']}：{gp['time']}（配速 {fmt_pace(gp['pace_s'])}/km），{cmp_txt}{gap2_txt}</strong>
        <p>取自 Garmin Connect 成绩预测（综合 VO2max 与长期训练负荷）。与 Riegel 互为交叉校验：
           Riegel 只用本期最长单场样本，佳明则综合了历史负荷，两者差异属正常。</p>
      </div>"""

        pred_html = f"""
      <div class="metric-grid">
        <div><span>目标赛事</span><strong>{esc(goals.get('race_name') or '—')}</strong><small>{esc(goals.get('race_date') or '')} · {goals.get('race_distance_km')}km</small></div>
        <div><span>倒计时</span><strong class="accent-val">{pred['days_left'] if pred['days_left'] is not None else '—'} 天</strong><small>今日 {date.today().isoformat()}</small></div>
        <div><span>预测完赛</span><strong>{p['predicted_txt']}</strong><small>Riegel · 样本 {p['from_km']}km</small></div>
        <div><span>预测配速</span><strong>{p['pace_txt']}/km</strong><small>目标 {fmt_pace(pred.get('target_pace_s'))}/km</small></div>
      </div>
      <div class="analysis-card"><span class="kicker">RACE PREDICTION</span>
        <strong>按 {p['from_km']}km（{lb.fmt_time(p['from_time_s'])}）推算全马 {p['predicted_txt']}，比目标 {goals.get('target_finish')} {gap_txt}</strong>
        <p>Riegel 指数 1.06；长距离样本越长预测越可靠。当前 VO2max {ath.get('vo2max') or '—'}、
           乳酸阈心率 {ath.get('lt_hr') or '—'} bpm。赛前 3 周进入减量期（跑量降至峰值 60%，保留强度刺激）。</p>
      </div>{garmin_card}"""

    focus = ctx["focus_run"]
    focus_title = f"{focus['date']} · {esc(focus['name'] or focus['type_key'])} · {focus['distance_km']:.2f} km" if focus else "本期无跑步记录"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Garmin 全景训练分析 · {meta['start']} → {meta['end']}</title>
<style>
{css}
</style>
</head>
<body>
{cdn_fallback}

<div class="topbar">
  <div class="brand">
    <span class="brand-mark">◐</span>
    <div><strong>GARMIN PANORAMA</strong><small>TRAINING ANALYSIS</small></div>
  </div>
  <div class="privacy-pill"><span class="dot"></span>仅本地 · 数据来自 Garmin Connect</div>
</div>

<header class="stage">
  <div class="eyebrow">{meta['start']} — {meta['end']} · 共 {meta['days']} 天</div>
  <h1>全景训练分析</h1>
  <p class="sub">Garmin Connect · 配速/心率/技术/热适应/负荷/恢复 六维复盘</p>
  {weather_badge}
  <div class="metric-hero">
    <strong>{ov['total_km']:.1f}</strong><span class="unit">km</span>
    <span class="tagline">{ov['run_count']} 次跑步 · 平均 {fmt_pace(ov['avg_pace'])}/km · 平均心率 {ov['avg_hr'] or '—'}</span>
  </div>
  <div class="metric-grid">
    <div><span>总时长</span><strong>{fmt_dur(ov['total_moving_s'])}</strong><small>运动时长（不含停顿）</small></div>
    <div><span>最长一次</span><strong class="accent-val">{ov['longest_km']:.1f} km</strong><small>{esc(ov['longest_date'] or '')}</small></div>
    <div><span>总卡路里</span><strong>{ov['total_cal'] or '—'}</strong><small>kcal</small></div>
    <div><span>累计爬升</span><strong>{ov['total_elev'] or 0:.0f} m</strong><small>elevation gain</small></div>
  </div>
</header>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">01</span><span class="section-icon">◧</span><h2>训练总览</h2></div>
    <div class="section-meta">按日 / 按周 / 按类型</div>
  </div>
  <div class="chart-card">
    <h3>每日跑量与训练负荷</h3>
    <p class="hint">同日多次跑步已合并；柱=跑量(km)，线=训练负荷(TSS)</p>
    <div class="chart-wrap"><canvas id="c1"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>周跑量节奏</h3>
    <p class="hint">以周一为起点；虚线为目标周跑量</p>
    <div class="chart-wrap"><canvas id="c2"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>训练类型分布</h3>
    <p class="hint">按距离占比</p>
    <div class="chart-wrap"><canvas id="c3"></canvas></div>
  </div>
</section>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">02</span><span class="section-icon">◨</span><h2>焦点跑 · 逐公里技术分解</h2></div>
    <div class="section-meta">{focus_title}</div>
  </div>
  <div class="chart-card">
    <h3>逐公里配速与心率</h3>
    <p class="hint">柱=配速(秒/km，越低越快)，线=平均心率(bpm)</p>
    <div class="chart-wrap"><canvas id="c4"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>跑姿经济性：步频 / 步幅 / 垂直振幅</h3>
    <p class="hint">垂直振幅越低、步幅越稳 = 跑姿越经济</p>
    <div class="chart-wrap"><canvas id="c5"></canvas></div>
  </div>
  {'<div class="table-card"><table class="data-table"><thead><tr><th>km</th><th>配速</th><th>心率</th><th>步频</th><th>步幅(m)</th><th>垂直振幅(cm)</th><th>垂直比(%)</th><th>触地(ms)</th><th>功率(W)</th></tr></thead><tbody>' + km_rows + '</tbody></table></div>' if km_rows else ''}
</section>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">03</span><span class="section-icon">◈</span><h2>心率强度分布</h2></div>
    <div class="section-meta">Garmin 5 分区 · 按时间占比</div>
  </div>
  <div class="chart-card">
    <h3>各心率区间停留时间</h3>
    <p class="hint">判定有氧基础是否扎实：低强度(Z1-Z2)应占 60–80%</p>
    <div class="chart-wrap"><canvas id="c6"></canvas></div>
  </div>
  {'<div class="table-card"><table class="data-table"><thead><tr><th>区间</th><th>心率</th><th>时长</th><th>占比</th></tr></thead><tbody>' + zone_rows + '</tbody></table></div>' if zone_rows else ''}
</section>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">04</span><span class="section-icon">☀</span><h2>热适应与天气</h2></div>
    <div class="section-meta">Open-Meteo 历史归档 + 手表实测</div>
  </div>
  {heat_cards}
  <div class="chart-card">
    <h3>WBGT 热应激 vs 平均心率</h3>
    <p class="hint">柱=估算 WBGT(°C)，线=平均心率(bpm)</p>
    <div class="chart-wrap"><canvas id="c7"></canvas></div>
  </div>
  {'<div class="table-card"><table class="data-table"><thead><tr><th>日期</th><th>气温</th><th>湿度</th><th>风(km/h)</th><th>WBGT</th><th>体感</th><th>配速</th><th>心率</th><th>%LT</th><th>热折损</th></tr></thead><tbody>' + heat_table + '</tbody></table></div>' if heat_table else ''}
  <div class="deep-analysis-list">
    <div class="deep-analysis-card"><span>ACCLIMATION SIGNALS</span>
      <h3>跨日热适应信号</h3>
      <ul style="margin:8px 0 0;padding-left:18px;color:var(--muted);font-size:11px;line-height:1.7">{heat_signals}</ul>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">05</span><span class="section-icon">◑</span><h2>负荷与恢复</h2></div>
    <div class="section-meta">ACWR · 就绪度 · HRV / 静息心率 / 睡眠</div>
  </div>
  <div class="metric-grid">
    <div><span>急性负荷 (ATL)</span><strong>{data['training_status'].get('acute_load') or '—'}</strong><small>Garmin 官方</small></div>
    <div><span>慢性负荷 (CTL)</span><strong>{data['training_status'].get('chronic_load') or '—'}</strong><small>Garmin 官方</small></div>
    <div><span>ACWR</span><strong class="accent-val">{data['training_status'].get('acwr') or '—'}</strong><small>{lb.classify_acwr(data['training_status'].get('acwr'))[1]}</small></div>
    <div><span>训练状态</span><strong>{esc(lb.status_zh(data['training_status'].get('status_phrase')))}</strong><small>VO2max {ath.get('vo2max') or '—'}</small></div>
  </div>
  <div class="chart-card">
    <h3>HRV 与静息心率</h3>
    <p class="hint">线=夜间 HRV(ms)，柱=静息心率(bpm)；HRV 上行 + RHR 下行 = 恢复良好</p>
    <div class="chart-wrap"><canvas id="c8"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>睡眠时长与睡眠分数</h3>
    <p class="hint">柱=睡眠小时，线=睡眠分数</p>
    <div class="chart-wrap"><canvas id="c9"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Body Battery 与压力</h3>
    <p class="hint">柱=晨间 Body Battery，线=平均压力值</p>
    <div class="chart-wrap"><canvas id="c10"></canvas></div>
  </div>
</section>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">06</span><span class="section-icon">◉</span><h2>身体成分与能量</h2></div>
    <div class="section-meta">Katch-McArdle · 宏量目标</div>
  </div>
  {energy_html}
</section>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">07</span><span class="section-icon">◍</span><h2>目标与成绩预测</h2></div>
    <div class="section-meta">Riegel 公式</div>
  </div>
  {pred_html}
</section>

<section class="section">
  <div class="section-heading">
    <div><span class="section-number">08</span><span class="section-icon">◎</span><h2>教练建议</h2></div>
    <div class="section-meta">基于本期真实数据生成</div>
  </div>
  <div class="recommendation-list">
    {recs_html}
  </div>
</section>

<footer class="footer">
  数据来源：<b>Garmin Connect</b>（活动 / 逐公里 / 心率分区 / 训练状态 / HRV / 睡眠 / Body Battery）
  + <b>Open-Meteo</b>（训练当日真实天气，免 key 历史归档）。<br>
  配速一律采用<b>运动时长</b>（moving duration）计算，不含红灯、补给与休息停顿；
  热应激指标采用 WBGT 估算式（0.7×湿球 + 0.3×干球，无日照近似）。<br>
  生成时间 {meta['fetched_at']} · 报告仅本地保存，未上传任何第三方。
</footer>

<script>
{chartjs}
</script>
<script>
const C = {js['colors']};
Chart.defaults.color = C.muted;
Chart.defaults.borderColor = C.grid;
Chart.defaults.font.family = '"Inter", ui-sans-serif, "PingFang SC", sans-serif';

function mixY(opts) {{
  return Object.assign({{
    stacked: false,
    scales: {{
      x: {{ grid: {{ display: false }} }},
      y: {{ beginAtZero: false, grid: {{ color: C.grid }} }}
    }},
    plugins: {{ legend: {{ labels: {{ boxWidth: 10, font: {{ size: 10 }} }} }} }},
    maintainAspectRatio: false, responsive: true
  }}, opts);
}}

new Chart(document.getElementById('c1'), mixY({{
  data: {{
    labels: {js['day_labels']},
    datasets: [
      {{ type: 'bar', label: '跑量 km', data: {js['day_dist']}, backgroundColor: C.accent, borderRadius: 3, yAxisID: 'y' }},
      {{ type: 'line', label: '训练负荷', data: {js['day_load']}, borderColor: C.warn, backgroundColor: C.warn, tension: 0.3, yAxisID: 'y1' }}
    ]
  }},
  options: {{ scales: {{ y: {{ position: 'left', beginAtZero: true }}, y1: {{ position: 'right', beginAtZero: true, grid: {{ drawOnChartArea: false }} }} }} }}
}}));

new Chart(document.getElementById('c2'), mixY({{
  type: 'bar',
  data: {{ labels: {js['week_labels']}, datasets: [
    {{ label: '周跑量 km', data: {js['week_dist']}, backgroundColor: C.accent, borderRadius: 3 }},
    {{ label: '目标线', data: {js['week_target']}, type: 'line', borderColor: C.danger, borderDash: [4,4], pointRadius: 0 }}
  ]}}
}}));

new Chart(document.getElementById('c3'), {{
  type: 'doughnut',
  data: {{ labels: {js['type_labels']}, datasets: [{{ data: {js['type_dist']}, backgroundColor: [C.accent, C.cool, C.warn, C.danger, C.muted], borderColor: C.bg }}]}},
  options: {{ maintainAspectRatio: false, responsive: true, plugins: {{ legend: {{ position: 'right', labels: {{ boxWidth: 10, font: {{ size: 10 }} }} }} }} }}
}});

new Chart(document.getElementById('c4'), mixY({{
  data: {{
    labels: {js['km_labels']},
    datasets: [
      {{ type: 'bar', label: '配速 秒/km', data: {js['km_pace']}, backgroundColor: C.cool, borderRadius: 3, yAxisID: 'y' }},
      {{ type: 'line', label: '心率 bpm', data: {js['km_hr']}, borderColor: C.danger, tension: 0.3, yAxisID: 'y1' }}
    ]
  }},
  options: {{ scales: {{ y: {{ position: 'left', reverse: false }}, y1: {{ position: 'right', grid: {{ drawOnChartArea: false }} }} }} }}
}}));

new Chart(document.getElementById('c5'), mixY({{
  data: {{
    labels: {js['km_labels']},
    datasets: [
      {{ type: 'line', label: '步频 spm', data: {js['km_cad']}, borderColor: C.accent, tension: 0.3, yAxisID: 'y' }},
      {{ type: 'line', label: '步幅 m', data: {js['km_stride']}, borderColor: C.cool, tension: 0.3, yAxisID: 'y1' }},
      {{ type: 'bar', label: '垂直振幅 cm', data: {js['km_vo']}, backgroundColor: 'rgba(255,107,95,0.5)', borderRadius: 3, yAxisID: 'y1' }}
    ]
  }},
  options: {{ scales: {{ y: {{ position: 'left' }}, y1: {{ position: 'right', grid: {{ drawOnChartArea: false }} }} }} }}
}}));

new Chart(document.getElementById('c6'), {{
  type: 'bar',
  data: {{ labels: {js['zone_labels']}, datasets: [{{ label: '分钟', data: {js['zone_minutes']}, backgroundColor: [C.cool, C.accent, C.accent, C.warn, C.danger], borderRadius: 3 }}]}},
  options: mixY({{}}).options || {{}}
}});

new Chart(document.getElementById('c7'), mixY({{
  data: {{
    labels: {js['heat_labels']},
    datasets: [
      {{ type: 'bar', label: 'WBGT °C', data: {js['heat_wbgt']}, backgroundColor: C.warn, borderRadius: 3, yAxisID: 'y' }},
      {{ type: 'line', label: '平均心率', data: {js['heat_hr']}, borderColor: C.danger, tension: 0.3, yAxisID: 'y1' }}
    ]
  }},
  options: {{ scales: {{ y: {{ position: 'left' }}, y1: {{ position: 'right', grid: {{ drawOnChartArea: false }} }} }} }}
}}));

new Chart(document.getElementById('c8'), mixY({{
  data: {{
    labels: {js['rec_labels']},
    datasets: [
      {{ type: 'line', label: 'HRV ms', data: {js['rec_hrv']}, borderColor: C.cool, tension: 0.3, yAxisID: 'y' }},
      {{ type: 'bar', label: '静息心率 bpm', data: {js['rec_rhr']}, backgroundColor: 'rgba(214,255,100,0.45)', borderRadius: 3, yAxisID: 'y1' }}
    ]
  }},
  options: {{ scales: {{ y: {{ position: 'left' }}, y1: {{ position: 'right', grid: {{ drawOnChartArea: false }} }} }} }}
}}));

new Chart(document.getElementById('c9'), mixY({{
  data: {{
    labels: {js['rec_labels']},
    datasets: [
      {{ type: 'bar', label: '睡眠 h', data: {js['rec_sleep']}, backgroundColor: C.accent, borderRadius: 3, yAxisID: 'y' }},
      {{ type: 'line', label: '睡眠分数', data: {js['rec_score']}, borderColor: C.cool, tension: 0.3, yAxisID: 'y1' }}
    ]
  }},
  options: {{ scales: {{ y: {{ position: 'left' }}, y1: {{ position: 'right', grid: {{ drawOnChartArea: false }} }} }} }}
}}));

new Chart(document.getElementById('c10'), mixY({{
  data: {{
    labels: {js['rec_labels']},
    datasets: [
      {{ type: 'bar', label: 'Body Battery', data: {js['rec_bb']}, backgroundColor: C.cool, borderRadius: 3, yAxisID: 'y' }},
      {{ type: 'line', label: '平均压力', data: {js['rec_stress']}, borderColor: C.warn, tension: 0.3, yAxisID: 'y1' }}
    ]
  }},
  options: {{ scales: {{ y: {{ position: 'left', beginAtZero: true }}, y1: {{ position: 'right', beginAtZero: true, grid: {{ drawOnChartArea: false }} }} }} }}
}}));
</script>
</body>
</html>
"""
    return html


# --------------------------------------------------------------------------- #
# 质量门禁
# --------------------------------------------------------------------------- #
def self_check(html, ctx, out_path):
    """
    双重门禁：
      1) Node 解析所有内联 <script> —— JS 语法必须无误
      2) 图表数据完整性 —— 每个数组非空、非全 0、labels 与 data 长度一致
    """
    ok = True
    msgs = []

    # ---- 第一重：JS 语法 ----
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    node = "node"
    try:
        for i, sc in enumerate(scripts, 1):
            if len(sc.strip()) < 50:
                continue
            p = subprocess.run([node, "-e", f"new Function(require('fs').readFileSync(0,'utf8'))"],
                               input=sc, capture_output=True, text=True, timeout=60)
            if p.returncode != 0:
                ok = False
                msgs.append(f"[JS] 第 {i} 段脚本语法错误: {p.stderr.strip()[:200]}")
    except FileNotFoundError:
        msgs.append("[JS] 未找到 node，跳过语法检查")
    if not scripts:
        ok = False
        msgs.append("[JS] 没有找到任何内联脚本")

    # ---- 第二重：数据完整性 ----
    js = ctx["js"]
    pairs = [
        ("day_labels", "day_dist"), ("week_labels", "week_dist"),
        ("km_labels", "km_pace"), ("km_labels", "km_hr"),
        ("heat_labels", "heat_wbgt"), ("heat_labels", "heat_hr"),
        ("rec_labels", "rec_hrv"), ("rec_labels", "rec_sleep"),
        ("zone_labels", "zone_minutes"), ("type_labels", "type_dist"),
    ]
    for lk, dk in pairs:
        labels, data_ = js.get(lk), js.get(dk)
        if not labels:
            continue  # 该模块无数据，图表不渲染，跳过
        if not data_:
            ok = False
            msgs.append(f"[数据] {dk} 为空")
            continue
        if len(labels) != len(data_):
            ok = False
            msgs.append(f"[数据] {lk}({len(labels)}) 与 {dk}({len(data_)}) 长度不一致")
        if all((v is None or v == 0) for v in data_):
            ok = False
            msgs.append(f"[数据] {dk} 全为 0 —— 图表会是空的")

    # 日期标签格式必须符合 MM/DD
    for lab in (js.get("day_labels") or [])[:3]:
        if not re.match(r"^\d{2}/\d{2}$", lab or ""):
            ok = False
            msgs.append(f"[数据] 日期标签格式异常: {lab}（应为 MM/DD）")

    # 关键总览数字必须来自合并后的完整数据
    if ctx["overview"]["total_km"] <= 0:
        ok = False
        msgs.append("[数据] 总跑量为 0")

    status = "PASS" if ok else "FAIL"
    print(f"\n=== 质量门禁 {status} ===")
    for m in msgs:
        print("  " + m)
    if ok:
        print(f"  内联脚本 {len(scripts)} 段 · 图表数据 {len([1 for k in js if k.endswith('labels')])} 组 · 输出 {out_path}")
    return ok


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="生成 Garmin 全景训练分析 HTML 报告")
    ap.add_argument("input", nargs="?", help="fetch_data.py 产出的 JSON")
    ap.add_argument("--out", help="输出 HTML 路径")
    ap.add_argument("--focus-run", help="焦点跑 activityId（默认取距离最长的一次）")
    args = ap.parse_args()

    if args.input:
        in_path = Path(args.input)
    else:
        files = sorted(DATA_DIR.glob("garmin_*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            print("错误：data/ 下没有找到 garmin_*.json，请先运行 fetch_data.py")
            sys.exit(1)
        in_path = files[-1]

    data = json.loads(in_path.read_text(encoding="utf-8"))
    print(f"[..] 读取 {in_path}")

    days, runs = build_daily_stats(data)
    weeks = build_weeks(runs)

    # ---- 焦点跑 ----
    focus = None
    if args.focus_run:
        focus = next((r for r in runs if str(r["id"]) == str(args.focus_run)), None)
    if focus is None and runs:
        focus = max(runs, key=lambda r: r["distance_km"])

    # ---- 概览 ----
    total_km = sum(r["distance_km"] for r in runs)
    total_moving = sum(r["moving_s"] for r in runs)
    hrs = [(r["avg_hr"], r["moving_s"]) for r in runs if r.get("avg_hr") and r.get("moving_s")]
    avg_hr = round(sum(h * w for h, w in hrs) / sum(w for _h, w in hrs)) if hrs else None
    longest = max(runs, key=lambda r: r["distance_km"]) if runs else None
    overview = {
        "total_km": round(total_km, 2),
        "run_count": len(runs),
        "total_moving_s": total_moving,
        "avg_pace": round(total_moving / total_km) if total_km else None,
        "avg_hr": avg_hr,
        "total_cal": round(sum(r.get("calories") or 0 for r in data["activities"])),
        "total_elev": round(sum(r.get("elev_gain_m") or 0 for r in runs), 1),
        "longest_km": (longest["distance_km"] if longest else 0),
        "longest_date": (longest["date"] if longest else None),
    }

    # ---- 心率分区 ----
    zone_seconds, zone_bounds = aggregate_zones(runs)
    total_zone_s = sum(zone_seconds) or 1
    zone_dist = [round(s / total_zone_s * 100, 1) for s in zone_seconds]

    # ---- 热适应 ----
    lt_hr = data["athlete"].get("lt_hr") or 172
    heat_rows, (heat_enriched, heat_signals) = evaluate_heat(runs, lt_hr)
    heat_rows = heat_enriched

    # ---- 焦点跑逐公里 ----
    fs = (focus or {}).get("splits_km") or []
    tech = None
    if fs:
        cad = [s["cadence"] for s in fs if s.get("cadence")]
        stride = [s["stride_m"] for s in fs if s.get("stride_m")]
        vo = [s["vertical_osc_cm"] for s in fs if s.get("vertical_osc_cm")]
        vr = [s["vertical_ratio"] for s in fs if s.get("vertical_ratio")]
        gct = [s["gct_ms"] for s in fs if s.get("gct_ms")]
        n = max(len(stride), 1)
        head = stride[:max(n // 3, 1)]
        tail = stride[-max(n // 3, 1):]
        drop = ((sum(head) / len(head) - sum(tail) / len(tail)) / (sum(head) / len(head)) * 100) if head and tail and sum(head) else 0
        tech = {
            "cad_mean": sum(cad) / len(cad) if cad else 0,
            "stride_mean": sum(stride) / len(stride) if stride else 0,
            "vo_mean": sum(vo) / len(vo) if vo else 0,
            "vr_mean": sum(vr) / len(vr) if vr else 0,
            "gct_mean": sum(gct) / len(gct) if gct else 0,
            "stride_drop": drop,
        }

    # ---- 恢复 / 就绪度 / 能量 / 预测 ----
    rec = build_recovery(data)
    readiness = compute_readiness(data)
    energy = build_energy(data)
    prediction = build_prediction(data, runs)

    # ---- 类型分布 ----
    by_type = {}
    for a in data["activities"]:
        if not a.get("distance_km"):
            continue
        by_type[a["category"]] = by_type.get(a["category"], 0) + a["distance_km"]
    type_labels = [{"run": "跑步", "strength": "力量", "ride": "骑行", "swim": "游泳", "other": "其他"}.get(k, k) for k in by_type]
    type_dist = [round(v, 2) for v in by_type.values()]

    target_wk = (data["config"].get("goals") or {}).get("weekly_volume_target_km") or 0

    ctx = {
        "overview": overview,
        "focus_run": focus,
        "focus_splits": fs[:30],
        "focus_weather": (focus or {}).get("weather"),
        "zone_seconds": zone_seconds,
        "zone_bounds": zone_bounds,
        "zone_dist": zone_dist,
        "heat_rows": heat_rows,
        "heat_signals": heat_signals,
        "readiness": readiness,
        "energy": energy,
        "prediction": prediction,
        "tech_stability": tech,
        "weeks": weeks,
        "js": {
            "colors": CHART_COLORS,
            "day_labels": [d["label"] for d in days],
            "day_dist": [d["dist"] for d in days],
            "day_load": [d["load"] for d in days],
            "week_labels": [w["label"] for w in weeks],
            "week_dist": [w["dist"] for w in weeks],
            "week_target": [target_wk] * len(weeks),
            "type_labels": type_labels,
            "type_dist": type_dist,
            "km_labels": [str(s["km"]) for s in fs[:30]],
            "km_pace": [s["pace_s"] for s in fs[:30]],
            "km_hr": [s["avg_hr"] for s in fs[:30]],
            "km_cad": [s["cadence"] for s in fs[:30]],
            "km_stride": [s["stride_m"] for s in fs[:30]],
            "km_vo": [s["vertical_osc_cm"] for s in fs[:30]],
            "zone_labels": [f"Z{i+1}" for i in range(len(zone_seconds))],
            "zone_minutes": [round(s / 60, 1) for s in zone_seconds],
            "heat_labels": [r["label"] for r in heat_rows],
            "heat_wbgt": [round(r["wbgt"], 1) for r in heat_rows],
            "heat_hr": [r["avg_hr"] for r in heat_rows],
            "rec_labels": [r["label"] for r in rec],
            "rec_hrv": [r["hrv"] for r in rec],
            "rec_rhr": [r["rhr"] for r in rec],
            "rec_sleep": [r["sleep_h"] for r in rec],
            "rec_score": [r["sleep_score"] for r in rec],
            "rec_bb": [r["bb"] for r in rec],
            "rec_stress": [r["stress"] for r in rec],
        },
    }
    ctx["recommendations"] = build_recommendations(data, ctx)

    out_path = Path(args.out) if args.out else DATA_DIR / f"report_{data['meta']['start'].replace('-','')}_{data['meta']['end'].replace('-','')}.html"
    html = render_html(data, ctx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[OK] 报告已生成: {out_path} ({len(html)/1024:.0f} KB)")

    if not self_check(html, ctx, out_path):
        print("\n质量门禁未通过，请修复后再交付。")
        sys.exit(1)


if __name__ == "__main__":
    main()
