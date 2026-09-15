#!/usr/bin/env python3
"""
Garmin 全景训练分析 —— 数据拉取脚本
=====================================
拉取指定日期范围内的 Garmin Connect 数据，标准化后输出单个 JSON，供 generate_report.py 消费。

用法：
    python3 scripts/fetch_data.py 20260801 20260818            # 指定起止日（YYYYMMDD）
    python3 scripts/fetch_data.py --days 7                      # 最近 7 天（含今天）
    python3 scripts/fetch_data.py --days 30 --out data/aug.json # 自定义输出
    python3 scripts/fetch_data.py --days 7 --no-daily           # 跳过逐日体征（快）

输出 JSON 结构（见 README.md「数据契约」）：
    meta / athlete / training_status / activities[] / daily[]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_client import get_client, safe_call, dump_token  # noqa: E402

import httpx  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent
# 个人配置优先；不存在时回落到仓库自带的示例配置（clone 后开箱即用）
CONFIG_FILE = SKILL_ROOT / "config.json"
CONFIG_EXAMPLE = SKILL_ROOT / "config.example.json"
DATA_DIR = SKILL_ROOT / "data"


def load_config():
    """读取配置。个人 config.json 不存在时自动使用 config.example.json。"""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")), str(CONFIG_FILE)
    if CONFIG_EXAMPLE.exists():
        print(f"[..] 未找到 config.json，使用示例配置 {CONFIG_EXAMPLE.name}"
              f"（复制为 config.json 可填写个人资料）")
        return json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8")), str(CONFIG_EXAMPLE)
    raise FileNotFoundError(
        f"缺少配置文件：{CONFIG_FILE} 或 {CONFIG_EXAMPLE}")


# 视作「跑步类」的活动 typeKey（Garmin 类型树）
RUN_TYPE_KEYS = {
    "running", "trail_running", "track_running", "treadmill_running",
    "indoor_running", "virtual_running", "ultra_running", "obstacle_run",
}
STRENGTH_TYPE_KEYS = {"strength_training", "hiit", "pilates", "yoga"}
RIDE_TYPE_KEYS = {"cycling", "indoor_cycling", "road_bike", "mountain_biking", "gravel_cycling"}
SWIM_TYPE_KEYS = {"swimming", "lap_swimming", "open_water_swimming"}

WMO_CODES = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨(弱)", 53: "毛毛雨", 55: "毛毛雨(强)", 56: "冻毛毛雨", 57: "冻毛毛雨(强)",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨(强)",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


# --------------------------------------------------------------------------- #
# 工具函数：单位与格式化
# --------------------------------------------------------------------------- #
def fmt_pace(sec_per_km):
    if not sec_per_km or sec_per_km <= 0:
        return None
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"


def _fmt_hms(sec):
    """秒 → H:MM:SS（不足 1 小时则 M:SS）。用于成绩预测。"""
    sec = int(round(sec or 0))
    if sec <= 0:
        return None
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def to_meters(v):
    """步幅：Garmin 不同端点可能返回米(1.05)或厘米(105)，统一成米。"""
    if v is None:
        return None
    v = float(v)
    return v / 100.0 if v > 3 else v


def to_cm(v):
    """垂直振幅：可能返回米(0.086)或厘米(8.6)，统一成厘米。"""
    if v is None:
        return None
    v = float(v)
    return v * 100.0 if v < 0.5 else v


def norm_type(act):
    t = act.get("activityType") or {}
    key = (t.get("typeKey") or "").lower()
    if key in RUN_TYPE_KEYS:
        return "run", key
    if key in STRENGTH_TYPE_KEYS:
        return "strength", key
    if key in RIDE_TYPE_KEYS:
        return "ride", key
    if key in SWIM_TYPE_KEYS:
        return "swim", key
    return "other", key


# --------------------------------------------------------------------------- #
# 天气：Garmin 自带 → Open-Meteo 兜底
# --------------------------------------------------------------------------- #
def open_meteo_weather(lat, lon, date_str, hour, cfg):
    """Open-Meteo 历史归档接口（免 key）。返回 dict 或 None。"""
    if lat is None or lon is None:
        return None
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": date_str, "end_date": date_str,
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation,weather_code",
        "timezone": "Asia/Shanghai",
    }
    try:
        r = httpx.get(url, params=params, timeout=30)
        r.raise_for_status()
        h = r.json().get("hourly", {})
        times = h.get("time", [])
        idx = None
        for i, t in enumerate(times):
            if t.endswith(f"T{hour:02d}:00"):
                idx = i
                break
        if idx is None and times:
            idx = min(hour, len(times) - 1)
        if idx is None:
            return None
        return {
            "source": "open-meteo",
            "temp_c": h.get("temperature_2m", [None] * (idx + 1))[idx],
            "humidity": h.get("relative_humidity_2m", [None] * (idx + 1))[idx],
            "wind_kmh": h.get("wind_speed_10m", [None] * (idx + 1))[idx],
            "precip_mm": h.get("precipitation", [None] * (idx + 1))[idx],
            "code": h.get("weather_code", [None] * (idx + 1))[idx],
        }
    except Exception as e:  # noqa: BLE001
        print(f"    [天气] Open-Meteo 取数失败: {e}")
        return None


def garmin_weather(client, aid, device_temp=None):
    """
    Garmin 活动自带天气。
    ⚠️ 单位陷阱：即便账号设为 metric，该端点仍返回 **英制**（温度 °F、风速 mph）。
       判定规则：温度 > 55 一律视作 °F（人类不可能在 >55°C 跑步），按 (F-32)×5/9 转 °C；
       风速同步按 mph × 1.609 转 km/h（实测 7mph=11.3km/h，与 Open-Meteo 10.4km/h 吻合；
       若误按 m/s×3.6 会得到 25km/h，明显偏大）。
    """
    w = safe_call(client.get_activity_weather, aid, default=None, label=f"Garmin 天气 {aid}")
    if not w or w.get("temp") is None:
        return None
    try:
        temp = float(w["temp"])
    except (TypeError, ValueError):
        return None
    imperial = temp > 55
    temp_c = (temp - 32) * 5.0 / 9.0 if imperial else temp
    wind = w.get("windSpeed")
    wind_kmh = None
    if wind is not None:
        wind = float(wind)
        wind_kmh = wind * 1.609 if imperial else wind * 3.6
    return {
        "source": "garmin-device",
        "temp_c": round(temp_c, 1),
        "humidity": round(float(w["relativeHumidity"]), 1) if w.get("relativeHumidity") is not None else None,
        "wind_kmh": round(wind_kmh, 1) if wind_kmh is not None else None,
        "precip_mm": None,
        "code": (w.get("weatherTypeDTO") or {}).get("weatherCode"),
        "desc": (w.get("weatherTypeDTO") or {}).get("desc"),
        "device_temp_c": device_temp,
        "unit_hint": "imperial->metric" if imperial else "metric",
    }


def fetch_weather(client, act, cfg):
    """
    训练天气：主源 Open-Meteo（按 GPS 坐标 + 开始时刻，免 key，便于跨设备比较）；
    Garmin 手表天气 / 手表实测温度作为补充（device_temp_c），Open-Meteo 取不到时整体回退 Garmin。
    """
    aid = act.get("activityId")
    lat = act.get("startLatitude")
    lon = act.get("startLongitude")
    if not lat or not lon:  # 室内跑 / 无 GPS → 用常驻城市
        lat = cfg["location"]["latitude"]
        lon = cfg["location"]["longitude"]

    device_temp = act.get("averageTemperature")
    st = act.get("startTimeLocal") or ""
    try:
        dt = datetime.strptime(st.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        dt = None

    w = None
    if dt:
        w = open_meteo_weather(lat, lon, dt.strftime("%Y-%m-%d"), dt.hour, cfg)
        if w:
            w["desc"] = WMO_CODES.get(w.get("code"), "")
            w["hour"] = dt.hour

    gw = garmin_weather(client, aid, device_temp=device_temp) if aid else None
    if w:
        if gw:
            w["device_temp_c"] = gw.get("temp_c")
            w["garmin_desc"] = gw.get("desc")
    else:
        w = gw
    if w and w.get("device_temp_c") is None and device_temp is not None:
        w["device_temp_c"] = device_temp
    return w


def extract_splits(client, aid, dist_km):
    """
    逐公里分段。

    数据源优先级：
      1. `get_activity_splits` → `lapDTOs`：真正的每公里自动圈（推荐）
      2. `get_activity_typed_splits` → `splits`：按活动段切分（RWD_RUN/RWD_STAND），非每公里，仅作兜底

    ⚠️ lapDTOs 字段单位/命名（与 COROS 不同）：
      distance 米 | duration/movingDuration 秒 | averageSpeed m/s
      averageRunCadence spm | strideLength **厘米** | verticalOscillation **厘米**
      groundContactTime 毫秒 | verticalRatio 百分比

    ⚠️ 配速必须用 movingDuration（运动时长），不可用 elapsedDuration（含红灯/补给/休息）。
    """
    laps = None
    src = None
    sp = safe_call(client.get_activity_splits, aid, default=None, label=f"splits {aid}")
    if sp:
        laps = sp.get("lapDTOs") or []
        src = "lapDTOs"
    if not laps:
        ts = safe_call(client.get_activity_typed_splits, aid, default=None, label=f"typed splits {aid}")
        if ts:
            # 只保留有距离的 RUN 段，过滤 RWD_STAND 等 0 距离段
            laps = [x for x in (ts.get("splits") or []) if (x.get("distance") or 0) > 0]
            src = "typed-splits"
    if not laps:
        return [], None

    out = []
    for i, lp in enumerate(laps, 1):
        d = lp.get("distance") or 0
        if d <= 0:
            continue
        moving = lp.get("movingDuration") or lp.get("duration") or 0
        speed = lp.get("averageMovingSpeed") or lp.get("averageSpeed")  # m/s
        pace = (1000.0 / speed) if speed else (moving / (d / 1000.0) if moving else None)
        vo = lp.get("verticalOscillation")
        stride = lp.get("strideLength")
        out.append({
            "km": i,
            "distance_m": round(d, 1),
            "duration_s": round(moving, 1),
            "pace_s": round(pace, 1) if pace else None,
            "pace": fmt_pace(pace),
            "avg_hr": lp.get("averageHR"),
            "max_hr": lp.get("maxHR"),
            "cadence": round(lp.get("averageRunCadence"), 1) if lp.get("averageRunCadence") else None,
            "stride_m": round(to_meters(stride), 3) if stride else None,
            "vertical_osc_cm": round(to_cm(vo), 2) if vo else None,
            "vertical_ratio": round(lp.get("verticalRatio"), 2) if lp.get("verticalRatio") else None,
            "gct_ms": round(lp.get("groundContactTime"), 1) if lp.get("groundContactTime") else None,
            "elev_gain_m": lp.get("elevationGain"),
            "avg_power": lp.get("averagePower"),
            "norm_power": lp.get("normalizedPower"),
            "partial": d < 900,  # 收尾不足 1km 的尾巴
        })
    return out, src


# --------------------------------------------------------------------------- #
# 活动标准化
# --------------------------------------------------------------------------- #
def normalize_activity(client, act, cfg, with_detail=True):
    category, type_key = norm_type(act)
    dist_m = act.get("distance") or 0
    dist_km = dist_m / 1000.0 if dist_m else 0.0

    # 配速必须用「运动时长」，不可用 elapsed（含红灯/补给/休息）
    moving_s = act.get("movingDuration") or act.get("duration") or 0
    elapsed_s = act.get("elapsedDuration") or act.get("duration") or 0
    moving_pace = (moving_s / dist_km) if (dist_km and moving_s) else None
    elapsed_pace = (elapsed_s / dist_km) if (dist_km and elapsed_s) else None

    item = {
        "id": act.get("activityId"),
        "name": act.get("activityName") or "",
        "type_key": type_key,
        "category": category,
        "date": (act.get("startTimeLocal") or "")[:10],
        "start_time": act.get("startTimeLocal"),
        "distance_km": round(dist_km, 3),
        "moving_s": round(moving_s or 0),
        "elapsed_s": round(elapsed_s or 0),
        "moving_pace_s": round(moving_pace, 1) if moving_pace else None,
        "elapsed_pace_s": round(elapsed_pace, 1) if elapsed_pace else None,
        "moving_pace": fmt_pace(moving_pace),
        "avg_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"),
        "avg_cadence": act.get("averageRunningCadenceInStepsPerMinute"),
        "max_cadence": act.get("maxRunningCadenceInStepsPerMinute"),
        "stride_m": round(to_meters(act.get("averageStrideLength")), 3) if act.get("averageStrideLength") else None,
        "vertical_osc_cm": round(to_cm(act.get("avgVerticalOscillation")), 2) if act.get("avgVerticalOscillation") else None,
        "gct_ms": round(act.get("avgGroundContactTime"), 1) if act.get("avgGroundContactTime") else None,
        "avg_power": act.get("avgPower"),
        "norm_power": act.get("normalizedPower"),
        "elev_gain_m": act.get("elevationGain"),
        "elev_loss_m": act.get("elevationLoss"),
        "calories": act.get("calories"),
        "aerobic_te": act.get("aerobicTrainingEffect"),
        "anaerobic_te": act.get("anaerobicTrainingEffect"),
        "training_stress_score": act.get("trainingStressScore") or act.get("activityTrainingLoad"),
        "vo2max": act.get("vO2MaxValue"),
        "avg_temperature_c": act.get("averageTemperature"),
        "lat": act.get("startLatitude"),
        "lon": act.get("startLongitude"),
        "laps": act.get("lapCount"),
        "splits_km": [],
        "splits_source": None,
        "hr_zones": [],
        "hr_zone_total_s": 0,
        "weather": None,
        "exercise_sets": None,
    }

    if not with_detail or not item["id"]:
        return item

    aid = item["id"]

    # --- 逐公里分段 ---
    item["splits_km"], item["splits_source"] = extract_splits(client, aid, dist_km)

    # --- 心率分区（秒） ---
    hz = safe_call(client.get_activity_hr_in_timezones, aid, default=None, label=f"HR zones {aid}")
    if hz:
        for z in hz:
            item["hr_zones"].append({
                "zone": z.get("zoneNumber"),
                # ⚠️ 字段名是 secsInZone（不是 secondsInZone），写错会静默拿到全 0
                "seconds": round(z.get("secsInZone") or z.get("secondsInZone") or 0, 1),
                "low": z.get("zoneLowBoundary"),
                "high": z.get("zoneHighBoundary"),
            })
    item["hr_zone_total_s"] = round(sum(z["seconds"] for z in item["hr_zones"]), 1)

    # --- 力量训练组数 ---
    if category == "strength":
        sets = safe_call(client.get_activity_exercise_sets, aid, default=None, label=f"exercise sets {aid}")
        if sets:
            exercises = sets.get("exerciseSets", []) if isinstance(sets, dict) else []
            item["exercise_sets"] = [
                {
                    "name": (e.get("exerciseName") or "").strip(),
                    "category": (e.get("category") or "").strip(),
                    "sets": e.get("setsCount") or len(e.get("setList") or []) or 0,
                    "reps": e.get("totalReps"),
                    "duration_s": e.get("duration"),
                }
                for e in exercises
            ]

    # --- 天气 ---
    item["weather"] = fetch_weather(client, act, cfg)
    return item


# --------------------------------------------------------------------------- #
# 运动员档案 / 训练状态
# --------------------------------------------------------------------------- #
def fetch_profile(client, cfg, start_date, end_date):
    prof = safe_call(client.get_user_profile, default={}, label="user profile") or {}
    athlete = {
        "name": prof.get("fullName") or cfg["athlete"].get("name"),
        "sex": cfg["athlete"].get("sex"),
        "age": cfg["athlete"].get("age"),
        "height_cm": cfg["athlete"].get("height_cm"),
        "weight_kg": cfg["athlete"].get("weight_kg"),
        "bodyfat_pct": cfg["athlete"].get("bodyfat_pct"),
        "max_hr": cfg["athlete"].get("max_hr"),
        "lt_hr": cfg["athlete"].get("lt_hr"),
        "resting_hr": cfg["athlete"].get("resting_hr"),
        "vo2max": None,
        "fitness_age": None,
        "race_predictions": {},
        "endurance_score": None,
        "hill_score": None,
        "hr_zones": [],
        "weight_source": "config" if cfg["athlete"].get("weight_kg") else None,
    }

    # 体重 / 体脂：优先 Garmin 体脂秤最新记录
    bc = safe_call(client.get_body_composition, (date.today() - timedelta(days=60)).isoformat(),
                   end_date, default=None, label="body composition")
    if bc and isinstance(bc, dict):
        items = (bc.get("dateWeightList") or bc.get("dateWeightSummaries") or [])
        valid = [x for x in items if x.get("weight")]
        if valid:
            latest = valid[-1]
            w_g = latest.get("weight")
            athlete["weight_kg"] = round(w_g / 1000.0, 2) if w_g else athlete["weight_kg"]
            # 体脂：体脂秤可能没有数据，缺失时保留 config 里的估算值
            athlete["bodyfat_pct"] = latest.get("bodyFat") or athlete["bodyfat_pct"]
            athlete["weight_source"] = f"garmin:{latest.get('samplePk') and latest.get('calendarDate') or 'latest'}"
            athlete["weight_date"] = latest.get("calendarDate")

    mm = safe_call(client.get_max_metrics, end_date, default=None, label="max metrics")
    if isinstance(mm, list) and mm:
        mm = mm[0]
    if isinstance(mm, dict):
        g = mm.get("generic") or {}
        athlete["vo2max"] = g.get("vo2MaxPreciseValue") or g.get("vo2MaxValue")
        athlete["fitness_age"] = g.get("fitnessAge")

    # ⚠️ 成绩预测返回的是「单个 dict」（不是 list）：
    #    {"time5K":1555,"time10K":3361,"timeHalfMarathon":7708,"timeMarathon":17325}（秒）
    #    早期固件可能返回 list[{raceDistanceInMeters, raceTimeinSeconds}]，两种都要兼容。
    preds = safe_call(client.get_race_predictions, default=None, label="race predictions")
    out = {}
    if isinstance(preds, dict):
        for key, label in (("time5K", "5K"), ("time10K", "10K"),
                           ("timeHalfMarathon", "半马"), ("timeMarathon", "全马")):
            ts = preds.get(key)
            if ts:
                out[label] = _fmt_hms(ts)
        # 同时保留秒数，便于报告里算配速
        athlete["race_prediction_sec"] = {
            lbl: preds.get(k) for k, lbl in (
                ("time5K", "5K"), ("time10K", "10K"),
                ("timeHalfMarathon", "半马"), ("timeMarathon", "全马")) if preds.get(k)
        }
    elif isinstance(preds, list):
        for r in preds:
            if not isinstance(r, dict):
                continue
            dk = (r.get("raceDistanceInMeters") or 0) / 1000.0
            ts = r.get("raceTimeinSeconds") or 0
            if dk > 0 and ts > 0:
                out[f"{dk:g}km"] = _fmt_hms(ts)
    if out:
        athlete["race_predictions"] = out

    # 耐力/爬坡分数：部分账号无数据（enduranceScoreDTO=null / hillScoreDTOList=[]），保持 None
    es = safe_call(client.get_endurance_score, start_date, end_date, default=None, label="endurance score")
    if isinstance(es, dict):
        dto = es.get("enduranceScoreDTO")
        if isinstance(dto, dict):
            athlete["endurance_score"] = dto.get("enduranceScore") or dto.get("value")
    elif isinstance(es, list) and es:
        athlete["endurance_score"] = es[-1].get("enduranceScore") or es[-1].get("value")
    hs = safe_call(client.get_hill_score, start_date, end_date, default=None, label="hill score")
    if isinstance(hs, dict):
        lst = hs.get("hillScoreDTOList") or []
        if lst:
            athlete["hill_score"] = lst[-1].get("hillScore") or lst[-1].get("value")
    elif isinstance(hs, list) and hs:
        athlete["hill_score"] = hs[-1].get("hillScore") or hs[-1].get("value")

    # 乳酸阈心率 / 功率
    # ⚠️ 结构是 speed_and_heart_rate（蛇形），不是驼峰；heartRate 单位 bpm
    lt = safe_call(client.get_lactate_threshold, default=None, label="lactate threshold")
    if isinstance(lt, dict):
        shr = lt.get("speed_and_heart_rate") or lt.get("speedAndHeartRate") or {}
        if isinstance(shr, dict) and shr.get("heartRate"):
            athlete["lt_hr"] = shr["heartRate"]
            athlete["lt_hr_date"] = (shr.get("calendarDate") or "")[:10]
        p = lt.get("power") or {}
        if isinstance(p, dict):
            athlete["lt_power"] = p.get("functionalThresholdPower")

    # 心率分区（Garmin 配置或按最大心率推导）
    hz = safe_call(client.get_heart_rate_zones, default=None, label="heart rate zones")
    zones = []
    if isinstance(hz, list) and hz:
        for z in hz:
            zones.append({
                "zone": z.get("zoneNumber"),
                "low": z.get("zoneLowBoundary"),
                "high": z.get("zoneHighBoundary"),
                "name": z.get("zoneName") or z.get("name"),
            })
    athlete["hr_zones"] = zones
    return athlete


def fetch_training_status(client, end_date):
    ts = safe_call(client.get_training_status, end_date, default=None, label="training status") or {}
    out = {
        "status": None, "status_phrase": None,
        "acute_load": None, "chronic_load": None, "acwr": None,
        "acwr_status": None, "load_ratio": None,
        "vo2max": None, "load_balance": None, "raw": ts,
    }
    # VO2max 藏在 mostRecentVO2Max.generic 里（账号级，不在 latestTrainingStatusData）
    v = (ts.get("mostRecentVO2Max") or {}).get("generic") or {}
    out["vo2max"] = v.get("vo2MaxPreciseValue") or v.get("vo2MaxValue")
    out["fitness_age"] = v.get("fitnessAge")

    # 训练状态：mostRecentTrainingStatus.latestTrainingStatusData.{deviceId}
    lts = (ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
    dev = None
    for _k, dv in lts.items():
        if isinstance(dv, dict):
            dev = dv if dev is None or dv.get("primaryTrainingDevice") else dev
    if isinstance(dev, dict):
        out["status"] = dev.get("trainingStatus")
        out["status_phrase"] = dev.get("trainingStatusFeedbackPhrase")
        dto = dev.get("acuteTrainingLoadDTO") or {}
        out["acute_load"] = dto.get("dailyTrainingLoadAcute")
        out["chronic_load"] = dto.get("dailyTrainingLoadChronic")
        out["acwr_status"] = dto.get("acwrStatus")
        # ⚠️ Garmin 的 acwrPercent 不是 acute/chronic 比值（曾出现 acute==chronic 而 acwrPercent=42），
        #    一律自行计算 ACWR = acute / chronic，避免误读。
        if out["acute_load"] and out["chronic_load"]:
            out["acwr"] = round(out["acute_load"] / out["chronic_load"], 2)
        out["load_tunnel_min"] = dev.get("loadTunnelMin")
        out["load_tunnel_max"] = dev.get("loadTunnelMax")
        out["fitness_trend"] = dev.get("fitnessTrend")

    # 负荷平衡（月度有氧低/高、无氧 + 目标区间）
    lbd = (ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
    for _k, dv in lbd.items():
        if isinstance(dv, dict):
            out["load_balance"] = {k: dv.get(k) for k in (
                "monthlyLoadAerobicLow", "monthlyLoadAerobicHigh", "monthlyLoadAnaerobic",
                "monthlyLoadAerobicLowTargetMin", "monthlyLoadAerobicLowTargetMax",
                "monthlyLoadAerobicHighTargetMin", "monthlyLoadAerobicHighTargetMax",
                "monthlyLoadAnaerobicTargetMin", "monthlyLoadAnaerobicTargetMax",
                "trainingBalanceFeedbackPhrase")}
            break
    return out


def infer_hr_zones(activities, athlete):
    """
    Garmin 的 get_heart_rate_zones 对部分账号只返回空壳（zone/low 全 None）。
    从活动明细里收集真实分区边界兜底：取出现次数最多的一套 low 边界。
    """
    seen = {}
    for a in activities:
        lows = tuple(z.get("low") for z in a.get("hr_zones") or [] if z.get("low"))
        if len(lows) >= 4:
            seen[lows] = seen.get(lows, 0) + 1
    if not seen:
        return []
    best = max(seen.items(), key=lambda kv: kv[1])[0]
    zones = []
    for i, low in enumerate(best, 1):
        high = best[i] if i < len(best) else None
        zones.append({"zone": i, "low": low, "high": (high - 1) if high else None,
                      "name": f"Z{i}"})
    return zones


# --------------------------------------------------------------------------- #
# 逐日体征
# --------------------------------------------------------------------------- #
def fetch_daily(client, start_date, end_date, with_detail=True):
    daily = []
    d0 = datetime.strptime(start_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(end_date, "%Y-%m-%d").date()

    # 批量接口：静息心率 / Body Battery
    rhr_map, bb_map = {}, {}
    rhr_list = safe_call(client.get_rhr_daily, start_date, end_date, default=None, label="RHR range")
    if isinstance(rhr_list, list):
        for r in rhr_list:
            if not isinstance(r, dict):
                continue
            key = (r.get("calendarDate") or r.get("date")) or ""
            val = r.get("restingHeartRate") or r.get("value")
            if key and val:
                rhr_map[str(key)[:10]] = val
    bb_list = safe_call(client.get_body_battery, start_date, end_date, default=None, label="body battery range")
    if isinstance(bb_list, list):
        for b in bb_list:
            if not isinstance(b, dict):
                continue
            key = (b.get("date") or b.get("calendarDate")) or ""
            if not key:
                continue
            bb_map[str(key)[:10]] = {
                "high": (b.get("chargedValue") or b.get("bodyBatteryChargedValue")),
                "low": (b.get("drainedValue") or b.get("bodyBatteryDrainedValue")),
            }

    d = d0
    while d <= d1:
        ds = d.isoformat()
        row = {
            "date": ds,
            "resting_hr": rhr_map.get(ds),
            "hrv": None, "hrv_status": None,
            "sleep_h": None, "sleep_score": None,
            "sleep_stages": None,
            "body_battery_high": bb_map.get(ds, {}).get("high"),
            "body_battery_low": bb_map.get(ds, {}).get("low"),
            "stress_avg": None, "stress_max": None,
            "steps": None, "distance_km": None,
            "calories_total": None, "calories_active": None,
            "readiness": None, "readiness_level": None,
            "acute_load": None,
            "spo2_avg": None,
        }

        if with_detail:
            stats = safe_call(client.get_stats_and_body, ds, default=None, label="")
            if isinstance(stats, dict):
                row["resting_hr"] = row["resting_hr"] or stats.get("restingHeartRate")
                row["stress_avg"] = stats.get("averageStressLevel")
                row["stress_max"] = stats.get("maxStressLevel")
                row["steps"] = stats.get("totalSteps")
                row["distance_km"] = round((stats.get("totalDistanceMeters") or 0) / 1000, 2) if stats.get("totalDistanceMeters") else None
                row["calories_total"] = stats.get("totalKilocalories")
                row["calories_active"] = stats.get("activeKilocalories")
                row["body_battery_high"] = row["body_battery_high"] or stats.get("bodyBatteryChargedValue")
                row["body_battery_low"] = row["body_battery_low"] or stats.get("bodyBatteryDrainedValue")

            hrv = safe_call(client.get_hrv_data, ds, default=None, label="")
            if isinstance(hrv, dict):
                summary = hrv.get("hrvSummary") or {}
                row["hrv"] = summary.get("lastNightAvg") or summary.get("lastNight")
                row["hrv_status"] = summary.get("status") or (summary.get("feedbackPhrase") or "").replace("HRV_", "")
                row["hrv_weekly_avg"] = summary.get("weeklyAvg") or summary.get("baselineLowUpper")

            sleep = safe_call(client.get_sleep_data, ds, default=None, label="")
            if isinstance(sleep, dict):
                daily_sleep = sleep.get("dailySleepDTO") or {}
                if daily_sleep:
                    row["sleep_h"] = round((daily_sleep.get("sleepTimeSeconds") or 0) / 3600, 2) if daily_sleep.get("sleepTimeSeconds") else None
                    row["sleep_score"] = (daily_sleep.get("sleepScores") or {}).get("overall", {}).get("value")
                    row["sleep_stages"] = {
                        "deep_s": daily_sleep.get("deepSleepSeconds"),
                        "light_s": daily_sleep.get("lightSleepSeconds"),
                        "rem_s": daily_sleep.get("remSleepSeconds"),
                        "awake_s": daily_sleep.get("awakeSleepSeconds") or daily_sleep.get("awakeDuration"),
                    }

            rd = safe_call(client.get_training_readiness, ds, default=None, label="")
            if isinstance(rd, list) and rd:
                rd = rd[0]
            if isinstance(rd, dict):
                row["readiness"] = rd.get("score")
                row["readiness_level"] = rd.get("level") or rd.get("readinessLevel")
                row["acute_load"] = rd.get("acuteTrainingLoad") or (rd.get("acuteTrainingLoadDTO") or {}).get("acuteTrainingLoad")

        daily.append(row)
        d += timedelta(days=1)

    return daily


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="拉取 Garmin Connect 训练数据")
    ap.add_argument("start", nargs="?", help="开始日期 YYYYMMDD")
    ap.add_argument("end", nargs="?", help="结束日期 YYYYMMDD")
    ap.add_argument("--days", type=int, help="最近 N 天（含今天），与 start/end 二选一")
    ap.add_argument("--out", help="输出 JSON 路径")
    ap.add_argument("--no-daily", action="store_true", help="跳过逐日体征（快）")
    ap.add_argument("--no-detail", action="store_true", help="跳过逐活动明细（最快）")
    args = ap.parse_args()

    if args.days:
        end_d = date.today()
        start_d = end_d - timedelta(days=args.days - 1)
    elif args.start and args.end:
        start_d = datetime.strptime(args.start, "%Y%m%d").date()
        end_d = datetime.strptime(args.end, "%Y%m%d").date()
    elif args.start:
        start_d = datetime.strptime(args.start, "%Y%m%d").date()
        end_d = date.today()
    else:
        end_d = date.today()
        start_d = end_d - timedelta(days=6)

    start_date, end_date = start_d.isoformat(), end_d.isoformat()
    cfg, cfg_src = load_config()
    print(f"[..] 配置来源: {Path(cfg_src).name}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else DATA_DIR / f"garmin_{start_d.strftime('%Y%m%d')}_{end_d.strftime('%Y%m%d')}.json"

    print(f"=== Garmin 数据拉取 {start_date} → {end_date} ===")
    client = get_client()

    athlete = fetch_profile(client, cfg, start_date, end_date)
    print(f"[..] 档案: 体重 {athlete.get('weight_kg')}kg")

    print("[..] 拉取活动列表...")
    raw = safe_call(client.get_activities_by_date, start_date, end_date, default=[], label="activities") or []
    print(f"     共 {len(raw)} 条活动")

    activities = []
    for i, act in enumerate(raw, 1):
        item = normalize_activity(client, act, cfg, with_detail=not args.no_detail)
        activities.append(item)
        print(f"     [{i}/{len(raw)}] {item['date']} {item['category']:8s} {item['distance_km']:>6.2f}km "
              f"{item['moving_pace'] or '-':>6s} HR{item['avg_hr'] or '-'}")

    # 心率分区边界：Garmin 配置接口常返回空壳，用活动明细兜底
    if not any(z.get("low") for z in athlete.get("hr_zones") or []):
        athlete["hr_zones"] = infer_hr_zones(activities, athlete)
        if athlete["hr_zones"]:
            athlete["hr_zone_source"] = "activity-inferred"

    print("[..] 拉取训练状态与负荷...")
    tstatus = fetch_training_status(client, end_date)

    # ⚠️ get_max_metrics 对部分账号返回 []，VO2max 要从训练状态里回填
    if not athlete.get("vo2max") and tstatus.get("vo2max"):
        athlete["vo2max"] = tstatus["vo2max"]
        athlete["vo2max_source"] = "training_status"
    if not athlete.get("fitness_age") and tstatus.get("fitness_age"):
        athlete["fitness_age"] = tstatus["fitness_age"]
    tz = athlete.get("hr_zone_source") or "garmin"
    print(f"[..] 档案: VO2max {athlete.get('vo2max')} / LT {athlete.get('lt_hr')}bpm / "
          f"分区 {len(athlete.get('hr_zones') or [])} 档({tz}) / 预测 {len(athlete.get('race_predictions') or {})} 项")

    if args.no_daily:
        daily = []
        print("[..] 跳过逐日体征")
    else:
        print("[..] 拉取逐日体征（HRV / 睡眠 / Body Battery / 就绪度）...")
        daily = fetch_daily(client, start_date, end_date)

    dump_token(client)

    payload = {
        "meta": {
            "start": start_date, "end": end_date,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "Garmin Connect (garminconnect)",
            "timezone": "Asia/Shanghai",
            "days": (end_d - start_d).days + 1,
        },
        "config": cfg,
        "athlete": athlete,
        "training_status": tstatus,
        "activities": activities,
        "daily": daily,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    runs = [a for a in activities if a["category"] == "run"]
    total = sum(a["distance_km"] for a in runs)
    print(f"\n[OK] 已保存: {out_path}")
    print(f"     跑步 {len(runs)} 次 / 总距离 {total:.1f} km / 逐日记录 {len(daily)} 天")


if __name__ == "__main__":
    main()
