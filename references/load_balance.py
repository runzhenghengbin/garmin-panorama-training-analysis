#!/usr/bin/env python3
"""
负荷平衡、就绪度与成绩预测模块（Garmin 版）
=============================================
数据源 = Garmin Connect 官方字段：
  - `training_status.acuteTrainingLoadDTO.dailyTrainingLoadAcute`   → 急性负荷（≈ATL）
  - `training_status.acuteTrainingLoadDTO.dailyTrainingLoadChronic` → 慢性负荷（≈CTL）
  - `daily[].hrv` / `resting_hr` / `sleep_score` / `body_battery_high` / `stress_avg`

对外函数（与 COROS 版 API 保持兼容，便于同一份报告模板复用）：
  acwr / classify_acwr / form / readiness / readiness_label
新增 Garmin 专有：
  status_zh / load_balance_zh / riegel / predict_race
"""

# ACWR 分档（Tim Gabbett 急性:慢性负荷比 + Garmin 负荷隧道口径）
ACWR_BANDS = [
    (0.0, 0.8, "detraining", "减量过度", "刺激不足，体能流失，可适度加量"),
    (0.8, 1.3, "balanced", "平衡", "负荷合理，可正常执行课表"),
    (1.3, 1.5, "rising", "快速上升", "负荷陡增，警惕过度训练，强度降档"),
    (1.5, 99.0, "excessive", "过高", "过度训练风险高，强制减量或休息"),
]

TRAINING_STATUS_ZH = {
    "NO_STATUS": "数据不足", "NO_STATUS_2": "数据累积中",
    "DETRAINING": "体能下降", "DETRAINING_2": "体能下降",
    "RECOVERY": "恢复中", "RECOVERY_2": "恢复中",
    "MAINTAINING": "状态保持", "MAINTAINING_2": "状态保持",
    "PRODUCTIVE": "高效进步", "PRODUCTIVE_2": "高效进步",
    "OVERREACHING": "过度负荷", "OVERREACHING_2": "过度负荷",
    "PEAKING": "巅峰状态", "PEAKING_2": "巅峰状态",
    "UNPRODUCTIVE": "低效训练", "UNPRODUCTIVE_2": "低效训练",
    "STRAINED": "身体紧张", "STRAINED_2": "身体紧张",
}

LOAD_BALANCE_ZH = {
    "BALANCED": "负荷均衡",
    "AEROBIC_LOW_SHORTAGE": "低强度有氧不足（基础耐力欠缺）",
    "AEROBIC_LOW_EXCESS": "低强度有氧偏多",
    "AEROBIC_HIGH_SHORTAGE": "高强度有氧不足（缺少速度刺激）",
    "AEROBIC_HIGH_EXCESS": "高强度有氧偏多",
    "ANAEROBIC_SHORTAGE": "无氧刺激不足",
    "ANAEROBIC_EXCESS": "无氧刺激偏多",
}


def acwr(acute, chronic):
    """ACWR = 急性负荷 / 慢性负荷。任一缺失返回 None。

    ⚠️ 不要直接用 Garmin 的 `acwrPercent`：它存在 acute==chronic 却报 42 的情况，
       语义并非 acute/chronic 比值，一律自行相除。
    """
    if not acute or not chronic:
        return None
    return round(acute / chronic, 2)


def classify_acwr(ratio):
    if ratio is None:
        return ("unknown", "未知", "缺少负荷数据")
    for lo, hi, key, name, advice in ACWR_BANDS:
        if lo <= ratio < hi:
            return (key, name, advice)
    return ("unknown", "未知", "超出常规区间")


def form(chronic, acute):
    """Form = 慢性负荷 - 急性负荷（体能储备；正=状态盈余，负=疲劳累积）。"""
    if chronic is None or acute is None:
        return None
    return round(chronic - acute, 1)


def status_zh(phrase):
    return TRAINING_STATUS_ZH.get((phrase or "").upper(), phrase or "—")


def load_balance_zh(phrase):
    return LOAD_BALANCE_ZH.get((phrase or "").upper(), phrase or "—")


def readiness(sleep_score=None, hrv=None, hrv_base=None, rhr=None, rhr_base=None,
              bb_high=None, stress_avg=None, acute=None, chronic=None,
              w_sleep=0.30, w_hrv=0.25, w_rhr=0.15, w_load=0.15, w_stress=0.15):
    """
    每日就绪度 0-100（R4F SDD §4.4 加权模型的 Garmin 落地版）。
    权重默认：睡眠 30% / HRV 25% / RHR 15% / 负荷平衡 15% / 压力 15%。
    缺失维度自动按剩余权重归一化，不臆造分数。
    返回 (score, parts)；parts = {维度: (权重, 得分)}
    """
    parts = {}

    if sleep_score is not None:
        parts["睡眠"] = (w_sleep, max(0, min(100, float(sleep_score))))

    if hrv is not None and hrv_base:
        off = (hrv - hrv_base) / hrv_base * 100
        score = 50 + off * 1.5          # +10% → 65 分；-10% → 35 分
        parts["HRV偏移"] = (w_hrv, max(0, min(100, round(score))))

    if rhr is not None and rhr_base:
        diff = rhr - rhr_base           # 高于基线 = 疲劳
        score = 70 - diff * 5
        parts["RHR偏移"] = (w_rhr, max(0, min(100, round(score))))

    ratio = acwr(acute, chronic)
    if ratio is not None:
        if ratio < 0.8:
            score = 60
        elif ratio < 1.3:
            score = 85
        elif ratio < 1.5:
            score = 50
        else:
            score = 20
        parts["负荷平衡"] = (w_load, score)

    if stress_avg is not None:
        score = 100 - float(stress_avg) * 2.0   # 0→100, 25→50, 50→0
        parts["压力"] = (w_stress, max(0, min(100, round(score))))

    # Body Battery 晨值作为恢复的直接证据（有则并入睡眠维度权重的一半）
    if bb_high is not None and "睡眠" in parts:
        w = parts["睡眠"][0]
        blended = round(parts["睡眠"][1] * 0.6 + float(bb_high) * 0.4)
        parts["睡眠"] = (w, max(0, min(100, blended)))

    if not parts:
        return None, {}
    total_w = sum(w for w, _ in parts.values())
    score = sum(w * s for w, s in parts.values()) / total_w
    return round(score), parts


def readiness_label(score):
    if score is None:
        return "数据不足"
    if score >= 80:
        return "就绪 · 可上强度"
    if score >= 65:
        return "良好 · 正常训练"
    if score >= 50:
        return "一般 · 保守执行"
    return "需恢复 · 降档或休息"


def riegel(t1_s, d1_km, d2_km, exponent=1.06):
    """Riegel 公式：由已知距离成绩预测另一距离完赛时间（秒）。"""
    if not t1_s or not d1_km or not d2_km:
        return None
    return t1_s * (d2_km / d1_km) ** exponent


def fmt_time(sec):
    if not sec:
        return "—"
    sec = int(round(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    s = sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace(sec_per_km):
    if not sec_per_km:
        return "—"
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}"


def predict_race(best_runs, target_km=42.195):
    """
    用区间内最好的几次跑（距离、耗时）按 Riegel 预测目标距离完赛时间与配速。
    best_runs: [(dist_km, moving_s), ...]
    返回 {"from_km", "predicted_s", "pace_s", "samples"}
    """
    if not best_runs:
        return None
    # 取最长的一次作为基准（长距离样本对马拉松预测更稳）
    base = max(best_runs, key=lambda x: x[0])
    d1, t1 = base
    if d1 < 3:
        return None
    pred = riegel(t1, d1, target_km)
    if not pred:
        return None
    return {
        "from_km": round(d1, 2),
        "from_time_s": round(t1),
        "predicted_s": round(pred),
        "predicted_txt": fmt_time(pred),
        "pace_s": round(pred / target_km, 1),
        "pace_txt": fmt_pace(pred / target_km),
    }


if __name__ == "__main__":
    print("== 自测：Garmin 真实字段口径 ==")
    r = acwr(678, 678)
    key, name, advice = classify_acwr(r)
    print(f"ACWR {r} → {name} / Form {form(678, 678):+}")
    sc, parts = readiness(sleep_score=95, hrv=65, hrv_base=56, rhr=46, rhr_base=49,
                          stress_avg=14, bb_high=33, acute=678, chronic=678)
    print(f"就绪度 {sc} → {readiness_label(sc)}")
    print("  " + " | ".join(f"{k}:{s}分(w{w*100:.0f}%)" for k, (w, s) in parts.items()))
    p = predict_race([(22.5, 9655)])
    print(f"Riegel: 22.5km/{fmt_time(9655)} → 全马 {p['predicted_txt']} @ {p['pace_txt']}/km")
