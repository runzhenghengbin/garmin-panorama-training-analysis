---
name: garmin-panorama-training-analysis
description: "基于佳明（Garmin Connect）数据的全景训练分析。拉取活动/逐公里明细(lapDTOs)/每日恢复(HRV·RHR·睡眠·Body Battery)/负荷平衡(ACWR·Form·就绪度)，融合 Open-Meteo 天气，输出 Ride Relief 暗黑风格离线 HTML 报告。能力：逐公里技术分解(配速/心率/步频/步幅/垂直振幅/触地时间)、心率5区训练比例、热适应评价(WBGT/露点/湿球/酷热指数)、训练状态与成绩预测(Riegel)、身体成分与能量。触发词：佳明分析、Garmin 训练报告、跑步分析、周报月报、热适应、LSD分析、负荷分析、ACWR、就绪度、训练状态。"
agent_created: true
---

# 基于佳明数据的全景训练分析（Garmin Panorama Training Analysis）

拉取佳明（Garmin Connect）数据，融合天气与身体状态，产出多维度训练分析 HTML 报告。

> 本 skill 是 `coros-panorama-training-analysis`（高驰版）的佳明移植版，五大分析引擎与设计系统保持一致，
> 数据源从 COROS API 换成 Garmin Connect（`garminconnect` 0.3.11）。

## 能力全景（五大分析引擎）

| 引擎 | 数据源 | 核心产出 |
|------|--------|----------|
| 逐公里技术分解 | `get_activity_splits → lapDTOs` | 每公里配速/心率/步频/步幅/垂直振幅/触地时间/垂直比 |
| 心率 5 区比例 | `get_activity_hr_in_timezones` | Z1–Z5 各区间时长与占比 |
| 热适应评价 | Open-Meteo 归档 + `references/heat_metrics.py` | 露点/湿球/澳式体感/酷热指数/估算WBGT + 跨日适应信号 |
| 负荷平衡与就绪度 | `get_training_status` + `references/load_balance.py` | ACWR/Form/每日就绪度 0-100 + 分档建议 |
| 身体成分与能量 | `get_body_composition` + `references/body_composition.py` | LBM/BMR(Katch-McArdle)/TDEE/跑步消耗/宏量目标 |
| 天气（报告必含） | Open-Meteo 历史归档 + 手表实测 | 温度/湿度/风/降水/WMO 代码 + 手表 device_temp 校验 |
| 成绩预测 | `get_race_predictions` + `riegel()` | 5K/10K/半马/全马预测 vs 目标配速 |
| 报告呈现 | `references/ride_relief_style.css` + Chart.js 内联 | Ride Relief 暗黑风格 + 双重质量门禁 |

## When to Use

- 用户要求分析佳明 / Garmin 的训练数据
- 生成周报、月报、赛前总结
- 可视化跑量、心率、HRV、RHR、睡眠、训练负荷、Body Battery
- 任何涉及 Garmin Connect 数据拉取 + 图表可视化的请求

---

## 快速开始

```bash
SKILL=~/.workbuddy/skills/garmin-panorama-training-analysis
PY=python3      # Windows 上若不可用，换成 python 或你的 venv 解释器全路径

# 1) 拉数（8/1–8/18）
cd $SKILL && $PY scripts/fetch_data.py 20260801 20260818

# 2) 生成报告（自动找最新 JSON，自动选最长一次跑为焦点跑）
$PY scripts/generate_report.py

# 或一条龙指定
$PY scripts/fetch_data.py --days 14 && $PY scripts/generate_report.py
```

### 脚本参数

**`scripts/fetch_data.py`**
```
位置参数: start [end]           YYYYMMDD（只给 start 则到今天）
--days N                        最近 N 天（含今天），与 start/end 二选一
--out PATH                      输出 JSON 路径（默认 data/garmin_<start>_<end>.json）
--no-daily                      跳过逐日体征（HRV/睡眠/BB），快很多
--no-detail                     跳过逐活动明细（splits/HR分区/天气），最快
```

**`scripts/generate_report.py`**
```
位置参数: input                 fetch_data.py 产出的 JSON（默认取 data/ 下最新）
--out PATH                      输出 HTML 路径
--focus-run ID                  焦点跑 activityId（默认取距离最长的一次跑步）
```

### 运行环境

- Python 3.9+：`pip install garminconnect==0.3.11 httpx requests`
  （`garminconnect` 版本敏感，0.3.x 之外 API 签名可能不同）
- Node：用于质量门禁的 JS 语法检查（可选，没装则跳过该检查）

### 凭据配置（零配置复用）

`scripts/garmin_client.py` 按以下优先级找凭据，**环境变量 > 本 skill `.env` > 已安装 skill 的 `.env`**：

1. 环境变量 `GARMIN_EMAIL` / `GARMIN_PASSWORD` / `GARMIN_IS_CN`
2. 本 skill 根目录 `.env`
3. 自动发现（已存在的可复用）：
   - `~/.workbuddy/skills/run-coach__skillhub/.env`
   - `~/.workbuddy/skills/coros-mcp-energy-lab__skillhub/.env`

`.env` 格式：
```
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=your_password
GARMIN_IS_CN=1            # 中国区账号（connect.garmin.cn）必填
```

Token 缓存：`~/.workbuddy/skills/garmin-panorama-training-analysis/.garth`
（与 run-coach 的独立，互不覆盖；登录一次后续免密）
MFA：脚本会交互式提示输入短信/邮箱验证码，验证后自动缓存。

---

## 核心铁律：Garmin 数据读取必须准确

### 铁律 1：配速必须用 `movingDuration`，绝不用 `elapsedDuration`

`elapsedDuration` 包含红灯、补给、拉伸、暂停。用它算配速会得到「越跑越慢」的假象。
佳明字段里 **`movingDuration` 是运动时长**，所有配速/速度计算一律基于它。

```python
# ✅ 正确
pace_s = lap["movingDuration"] / (lap["distance"] / 1000)
# ❌ 错误（含红灯/补给）
pace_s = lap["elapsedDuration"] / (lap["distance"] / 1000)
```

### 铁律 2：天气端点返回**英制**，即使账号是公制 ⚠️ 头号陷阱

`get_activity_weather` 返回的 `temperature` 单位是 **°F**，`windSpeed` 是 **mph**。
如果直接当 °C 用，初秋一次普通训练会显示 73°C。

判定与转换（已实现在 `scripts/fetch_data.py::garmin_weather()`）：
```python
imperial = temp > 55                              # 没人会在 55°C 跑步
temp_c   = (temp - 32) * 5.0 / 9.0 if imperial else temp
wind_kmh = wind * 1.609 if imperial else wind * 3.6   # mph→km/h，不是 m/s×3.6！
```
> 实测校验：风速 7 mph → `7×1.609 = 11.3 km/h`，与 Open-Meteo 同期 10.4 km/h 吻合；
> 若误按 m/s×3.6 会得到 25.2 km/h，明显偏高。

**报告主用 Open-Meteo**（GPS 坐标 + 开始时刻，跨设备可比），
佳明手表天气降为交叉校验字段（`device_temp_c` / `garmin_desc`）。

### 铁律 3：HR 分区字段名是 `secsInZone`，不是 `secondsInZone` ⚠️ 静默全 0

写错不会报错，只会让所有分区秒数变成 0，图表全空。
```python
# ✅
"seconds": round(z.get("secsInZone") or z.get("secondsInZone") or 0, 1),
# 自检：hr_zone_total_s 应 ≈ 活动 movingDuration，否则说明取错了
```

### 铁律 4：逐公里 splits 用 `lapDTOs`，字段名是小写驼峰

`get_activity_typed_splits` 的 `splitSummaries` 对跑步常返回**空**，必须用 `get_activity_splits` 的 `lapDTOs`。
回退到 typed_splits 时要过滤 `distance == 0` 的 `RWD_STAND` 段。

`lapDTOs` 单段字段（单位极易搞错）：
| 字段 | 单位 | 标准化后字段 |
|------|------|------|
| `distance` | **米** | `distance_m` |
| `movingDuration` / `duration` | **秒** | `duration_s`（只用 moving） |
| `averageMovingSpeed` | **m/s** | 配速 = 1000/speed → `pace_s` / `pace` |
| `averageRunCadence` | **spm**（步/分） | `cadence` |
| `strideLength` | **米 or 厘米**（看固件） | `stride_m` |
| `verticalOscillation` | **米 or 厘米**（看固件） | `vertical_osc_cm` |
| `groundContactTime` | **毫秒** | `gct_ms` |
| `verticalRatio` | **%** | `vertical_ratio` |

单位自适应（已实现在 `scripts/fetch_data.py`）：
```python
def to_meters(v):     # 步幅：>3 视为厘米，否则视为米
    return v / 100.0 if v > 3 else v
def to_cm(v):         # 垂直振幅：<0.5 视为米，否则视为厘米
    return v * 100.0 if v < 0.5 else v
```
输出 JSON 里记 `splits_source`（`lapDTOs` / `typed-splits`）便于追溯。

### 铁律 5：**不要**直接用 Garmin 的 `acwrPercent`

Garmin 的 `acwrPercent` 是一个百分比排名/相对值，**不是** Gabbett 定义的急性:慢性比值。
必须自己算：
```python
acwr = round(acute_load / chronic_load, 2) if chronic_load else None
# acute  = mostRecentTrainingStatus... .acuteTrainingLoadDTO.dailyTrainingLoadAcute
# chronic= ... .dailyTrainingLoadChronic
```
分档（Gabbett）：`<0.8` 欠载 / `0.8–1.3` 平衡（甜区）/ `1.3–1.5` 偏高警戒 / `>1.5` 危险。

### 铁律 6： lactate threshold 结构是**蛇形** `speed_and_heart_rate`

```python
lt  = client.get_lactate_threshold(...)
shr = lt.get("speed_and_heart_rate") or lt.get("speedAndHeartRate") or {}
lt_hr    = shr.get("heartRate")                     # 实测 172 bpm
lt_pace  = shr.get("speed")                         # m/s
lt_power = (lt.get("power") or {}).get("functionalThresholdPower")   # 338 W
```
写成驼峰 + `[-1]` 当列表取，会得到 `None`。

### 铁律 7：VO2max / 训练状态 / 体脂的真实字段路径

用 COROS 的思维定式去猜会全部拿到 `None`。佳明的真实路径：

```python
# VO2max —— 藏在 mostRecentVO2Max.generic 里
mx = client.get_max_metrics()  # dict or list
vo2 = mx["mostRecentVO2Max"]["generic"]["vo2MaxPreciseValue"]      # 46.1

# 训练状态 —— 按 deviceId 分组，取最新
ts  = client.get_training_status(...)["mostRecentTrainingStatus"]["latestTrainingStatusData"]
sub = ts[max(ts, key=lambda d: ts[d].get("trainingStatusUpdateDate") or "")]

# 体脂 —— 体脂秤无记录时 fallback 到 config.json 的估算值
bodyfat = latest.get("bodyFat") or cfg["athlete"]["bodyfat_pct"]
```

### 铁律 8：`get_race_predictions` 返回**单个 dict**，不是 list ⚠️ 静默丢数据

```json
{"time5K":1555,"time10K":3361,"timeHalfMarathon":7708,"timeMarathon":17325}   // 单位：秒
```
按 list 迭代（`for r in preds`）会拿到字符串 key，`isinstance(r, dict)` 全 False → 静默得到 `{}`。
```python
for key, label in (("time5K","5K"), ("time10K","10K"),
                   ("timeHalfMarathon","半马"), ("timeMarathon","全马")):
    out[label] = _fmt_hms(preds.get(key))
# 同时存秒数版 race_prediction_sec，供报告算配速/差值
```

### 铁律 9：`get_max_metrics` 可能返回 `[]` → VO2max 要从训练状态回填

部分账号 `get_max_metrics(date)` 返回空列表，但 `get_training_status(date)`
里的 `mostRecentVO2Max.generic.vo2MaxPreciseValue` 有值（实测 46.1）。
`main()` 里已做回填，并标记 `vo2max_source: "training_status"`。

### 铁律 10：`endurance_score` / `hill_score` 对多数账号本就是 null

返回结构是 dict：`{"enduranceScoreDTO": null}` / `{"hillScoreDTOList": []}`。
**这不是 bug**，是账号没有耐力/爬坡分数据（需足够爬升与长距离样本）。保持 `None` 即可，别当成解析失败去"修"。

### 铁律 11：`get_endurance_score` / `get_hill_score` 需要**两个**日期参数

```python
# ❌ 报 missing 1 required positional argument: 'startdate'
client.get_endurance_score(end_date)
# ✅
client.get_endurance_score(start_date, end_date)
```
同理 `fetch_profile(client, cfg, start_date, end_date)` 签名要两个日期。

### 铁律 11：`get_heart_rate_zones()` 可能返回空壳 → 从活动推断

部分账号返回 `[{zone: None, low: None, high: None}, ...]`。
兜底方案 `infer_hr_zones()`：扫描所有活动明细里的 HR 分区 low 边界，取出现次数最多的一组，
并在输出里标记 `hr_zone_source: "activity-inferred"`，让用户知道这是推断值。

### 铁律 12：天气是报告的**必含**章节，不是可选项

每次训练都要带上：温度 / 相对湿度 / 风速 / 降水 / WMO 天气代码（中文描述）。
没有天气的跑步分析在热适应章节是残废的。

---

## 输出数据结构

`fetch_data.py` 产出的 JSON：
```jsonc
{
  "meta":   { "generated_at", "start_date", "end_date", "source": "garmin" },
  "config": { /* config.json 原样透传（运动员档案/目标/坐标/宏量） */ },
  "athlete": {
    "weight_kg", "height_cm", "age", "sex", "bodyfat_pct",
    "resting_hr", "max_hr", "lt_hr", "lt_pace_s", "lt_power_w",
    "vo2max", "vo2max_source", "fitness_age",
    "race_predictions": {"5K","10K","半马","全马"},      // H:MM:SS 文本
    "race_prediction_sec": {"5K":1555, ...},             // 秒数，供算配速/差值
    "hr_zones": [{"zone","low","high","name"}],
    "hr_zone_source": "garmin" | "activity-inferred",
    "training_status": "...", "acwr", "acute_load", "chronic_load",
    "load_balance": {"aerobic_low","aerobic_high","anaerobic"},
    "race_predictions": [...]
  },
  "activities": [ {
      "id", "name", "type_key", "category", "date", "start_time",
      "distance_km", "moving_s", "elapsed_s",
      "moving_pace_s", "moving_pace", "elapsed_pace_s",
      "avg_hr", "max_hr", "avg_cadence", "max_cadence",
      "stride_m", "vertical_osc_cm", "gct_ms",
      "elev_gain_m", "elev_loss_m", "avg_power", "norm_power",
      "calories", "aerobic_te", "anaerobic_te", "training_stress_score",
      "vo2max", "avg_temperature_c", "lat", "lon", "laps",
      "weather": { "temp_c","humidity","wind_kmh","precip_mm","code","desc",
                   "dew_point_c","wet_bulb_c","apparent_c","heat_index_c",
                   "est_wbgt","heat_category","device_temp_c","garmin_desc" },
      "hr_zones": [{"zone","low","high","seconds","pct"}], "hr_zone_total_s",
      "splits_km": [ {"km","distance_m","duration_s","pace_s","pace","avg_hr","max_hr",
                      "cadence","stride_m","vertical_osc_cm","vertical_ratio","gct_ms",
                      "elev_gain_m","avg_power","norm_power","partial"} ],
      "splits_source": "lapDTOs" | "typed-splits",
      "exercise_sets": [...]        // 仅力量训练
  } ],
  "daily": [ {
      "date", "resting_hr", "hrv", "hrv_status",
      "sleep_h", "sleep_score",
      "sleep_stages": {"deep","light","rem","awake"},
      "body_battery_high", "body_battery_low",
      "stress_avg", "stress_max",
      "steps", "distance_km", "calories_total", "calories_active",
      "readiness", "readiness_level", "acute_load"
  } ]
}
```

---

## 报告结构（8 章节 / 10 图表）

| 章节 | 内容 |
|------|------|
| 01 训练总览 | 总跑量/次数/时长/爬升 + 周跑量 vs 目标线 + 类型分布 |
| 02 焦点跑逐公里技术分解 | 每公里配速/心率 + 步频/步幅/垂直振幅三条技术曲线 |
| 03 心率强度分布 | Z1–Z5 时长占比（基于 `secsInZone`） |
| 04 热适应与天气 | 每次训练 WBGT + 平均心率双轴，跨日适应信号 |
| 05 负荷与恢复 | ACWR/Form/训练状态 + HRV·RHR + 睡眠时长·分数 + Body Battery·压力 |
| 06 身体成分与能量 | LBM/BMR/TDEE/跑步消耗/宏量目标 |
| 07 目标与成绩预测 | 佳明官方预测 + Riegel 预测 双轨对照 vs 目标配速差距 |
| 08 教练建议 | 全部从真实数据推导的规则化建议 |

图表：`c1` 每日跑量+负荷 / `c2` 周跑量+目标线 / `c3` 类型分布环形 /
`c4` 逐公里配速+心率 / `c5` 步频+步幅+垂直振幅 / `c6` 心率分区 /
`c7` WBGT+心率 / `c8` HRV+静息心率 / `c9` 睡眠时长+分数 / `c10` Body Battery+压力

---

## 双重质量门禁（生成后必须执行）

`generate_report.py` 结尾自动跑，失败会明确报错。**不允许手动跳过。**

**门禁 1 — JS 语法检查**：提取所有内联 `<script>`，用 Node 逐个 `new Function(...)` 解析。
（这一步拦住过 f-string 拼接炸掉的图表配置）

**门禁 2 — 图表数据完整性**：
- 每组 `data` 非空、非全 0
- `labels.length === data.length`
- 日期标签必须匹配 `^\d{2}/\d{2}$`（MM/DD）

通过样例输出：
```
=== 质量门禁 PASS ===
  内联脚本 2 段 · 图表数据 7 组 · 输出 .../data/report_20260901_20260915.html
```

---

## 实现陷阱（HTML 生成）

### ⚠️ JS 数组必须在 f-string 外先 `json.dumps()`

在外层 f-string 里再嵌一层 f-string 生成 JS 数组，会因为大括号 `{}` 与 f-string 冲突而炸。
**唯一正确写法**：
```python
js = {k: json.dumps(v, ensure_ascii=False) for k, v in ctx["js"].items()}
html = f"""
<script>
new Chart(ctx, {{ labels: {js['labels']}, datasets: [...] }});
</script>
"""
```

### ⚠️ 日期标签月/日不可颠倒

```python
def md_label(d):            # d = "2026-09-13"
    return f"{int(d[5:7]):02d}/{int(d[8:10]):02d}"     # → "09/13"
# ❌ 常见错误：写成 {d[8:10]}/{d[5:7]} → "13/09"，图形时序全反
```

### ⚠️ Chart.js 必须内联

`assets/chart.umd.min.js`（Chart.js 4.4.4，205 KB）在生成时内联进 HTML，
保证报告**离线可用**（不依赖 CDN）。`config.json` 的 `report.inline_chartjs: true` 控制。

---

## 设计系统（Ride Relief）

`references/ride_relief_style.css` — 暗黑 + 招牌青柠，与 COROS 版完全一致：

```css
--background: #0b0d0c;   --panel: #111411;   --panel-2: #161a17;
--ink: #f1f3e9;          --muted: #8b938a;
--accent: #d6ff64;       /* 招牌青柠 */
--danger: #ff6b5f;       --warn: #ffc857;    --cool: #72e8ff;
```

排版规则：卡片圆角 14px / 内边距 20px / 数字用 tabular-nums / 章节编号 01–08。
修改报告外观时**只改 CSS 变量**，不要散落硬编码颜色。

---

## 参考模块（`references/`）

| 模块 | 关键函数 |
|------|----------|
| `heat_metrics.py` | `dew_point` / `wet_bulb_stull` / `apparent_temp_au` / `heat_index` / `est_wbgt` / `heat_category` / `evaluate_run` / `acclimation_table` |
| `load_balance.py` | `acwr` / `classify_acwr` / `form` / `status_zh` / `load_balance_zh` / `readiness` / `readiness_label` / `riegel` / `fmt_time` / `fmt_pace` / `predict_race` |
| `body_composition.py` | `lbm` / `bmr_katch` / `bmr_mifflin` / `running_kcal` / `tdee` / `macro_targets` |

- **估算 WBGT** = `0.7 × Twb + 0.3 × T`（Stull 2011 湿球 + 干球加权）
- **就绪度权重**：睡眠 30% / HRV 25% / RHR 15% / 负荷平衡 15% / 压力 15%，缺失维度自动归一化
- **Riegel**：`T2 = T1 × (D2/D1)^1.06`
- **BMR**：Katch-McArdle `370 + 21.6 × LBM`

每个模块都有 `__main__` 自测，改完直接跑验证：
```bash
$PY references/load_balance.py
```

---

## config.json（运动员档案）

报告的所有个性化参数都在这里，**新增/修改目标只需改这一个文件**：

```jsonc
{
  "athlete":  { "name","sex","age","height_cm","weight_kg","bodyfat_pct","max_hr","lt_hr","resting_hr" },
  "location": { "name","latitude","longitude" },   // 天气查询坐标
  "goals":    { "race_name","race_date","race_distance_km",
                "target_finish","target_pace_sec_per_km",
                "stretch_finish","weekly_volume_target_km" },
  "hr_zone_source": "garmin",
  "manual_hr_zones": null,          // 想强制指定分区时填 [{"zone":1,"low":100,"high":120}, ...]
  "activity_factor": 1.55,          // TDEE 系数
  "macro": { "protein_g_per_kg":2.0, "carb_g_per_kg":5.0, "fat_g_per_kg":1.0 },
  "report": { "style":"ride-relief", "inline_chartjs": true }
}
```
`weight_kg` / `max_hr` / `lt_hr` / `resting_hr` 留 `null` 时自动从 Garmin 拉取覆盖。

---

## 常见故障排查

| 症状 | 原因 | 修复 |
|------|------|------|
| `missing 1 required positional argument: 'startdate'` | 只传了一个日期 | 铁律 11：补 `start_date` |
| splits 全 `distance_m: 0` | 用了 typed_splits 的 `splitSummaries` | 铁律 4：改 `lapDTOs` |
| HR 分区秒数全 0 | 写成 `secondsInZone` | 铁律 3：改 `secsInZone` |
| 步幅 105（应为 1.05m） | 厘米未换算 | 铁律 4：`to_meters()`（>3 视为厘米） |
| 天气 73（应为 26） | 英制 °F 未转换 | 铁律 2：`temp > 55` 判 °F |
| 风 25.2 km/h 明显偏高 | mph 误当 m/s ×3.6 | 铁律 2：`*1.609` |
| VO2max / 训练状态 / 负荷全 None | 字段路径猜错 | 铁律 7：用真实路径 |
| `athlete.vo2max` 为 None | `get_max_metrics` 返回 `[]` | 铁律 9：从 `training_status` 回填 |
| 成绩预测 `{}` 全空 | 把 dict 当 list 迭代 | 铁律 8：按 `time5K/time10K/...` 取 |
| 耐力/爬坡分 None | 账号本就无此数据 | 铁律 10：正常，不是 bug |
| `lt_hr` 为 None | 驼峰 + 当列表取 | 铁律 6：蛇形 `speed_and_heart_rate` |
| 429 限流报错 | 请求过密 | `safe_call()` 已内置 2s/6s/18s 退避重试；仍失败则等几分钟 |
| 认证失败 | token 过期/MFA | 删 `.garth` 重跑，重新过验证码 |
| 报告图表空白 | JS 数组构造错误 | 门禁 1 会拦；检查是否在 f-string 内嵌 f-string |

---

## 与 COROS 版的差异

| 维度 | COROS 版 | Garmin 版 |
|------|----------|-----------|
| 客户端 | 自建 HTTP + token | `garminconnect` 0.3.11 |
| 逐公里 | `activity/detail/query` | `get_activity_splits → lapDTOs` |
| HR 分区 | `frequencyList` 6 区 | `get_activity_hr_in_timezones` 5 区（`secsInZone`） |
| 负荷 | ATI / CTI | `dailyTrainingLoadAcute` / `Chronic`（自算 ACWR） |
| 天气 | Open-Meteo | Open-Meteo 主用 + 手表 `device_temp_c` 校验（需转英制） |
| 恢复 | COROS 自有 | HRV / RHR / 睡眠分期 / Body Battery / 压力 / 训练就绪度 |
| 特色 | — | 成绩预测、乳酸阈功率、耐力/爬坡分数、力量组次 |

---

## 目录结构

```
garmin-panorama-training-analysis/
├── SKILL.md                       本文件
├── README.md                      人类可读说明
├── config.json                    运动员档案 / 目标 / 坐标
├── .env                           凭据（不入库）
├── .garth/                        token 缓存（不入库）
├── scripts/
│   ├── garmin_client.py           凭据解析 + 登录 + safe_call 限流重试
│   ├── fetch_data.py              拉数 → 标准化 JSON
│   └── generate_report.py         JSON → HTML 报告 + 双重质量门禁
├── references/
│   ├── heat_metrics.py            热适应计算
│   ├── load_balance.py            ACWR / Form / 就绪度 / Riegel
│   ├── body_composition.py        LBM / BMR / TDEE / 宏量
│   └── ride_relief_style.css      设计系统
├── assets/
│   └── chart.umd.min.js           Chart.js 4.4.4（离线内联）
└── data/                          产出（JSON + HTML）
```
