# Garmin Panorama Training Analysis

基于佳明（Garmin Connect）数据的全景训练分析 —— 拉数、融合天气、产出一页式暗黑风 HTML 报告。

> 移植自 [coros-panorama-training-analysis](https://github.com/yoyojay/coros-panorama-training-analysis)，
> 五大分析引擎与设计系统保持一致，数据源换成 Garmin Connect。

## 安装

一行命令装到 WorkBuddy（其他 Agent 把路径换成各自的 skills 目录即可）：

```bash
git clone https://github.com/runzhenghengbin/garmin-panorama-training-analysis.git \
  ~/.workbuddy/skills/garmin-panorama-training-analysis
```

Windows（PowerShell）:

```powershell
git clone https://github.com/runzhenghengbin/garmin-panorama-training-analysis.git "$env:USERPROFILE\.workbuddy\skills\garmin-panorama-training-analysis"
```

然后装依赖（Python 3.9+）：

```bash
pip install garminconnect==0.3.11 httpx requests
```

> 报告生成阶段还需要 **Node.js**（用于内联 JS 的语法门禁）。没装也能跑，只是跳过该检查。

## 它做什么

1. 登录 Garmin Connect，拉取指定区间的活动 + 逐日体征
2. 对每次跑步补齐：逐公里分段、心率分区、天气（Open-Meteo + 手表实测）
3. 计算：热适应指标、ACWR 负荷平衡、就绪度、Riegel 成绩预测、能量与宏量
4. 生成一份 **离线可用** 的 HTML 报告（Chart.js 已内联，不联网也能打开）

## 报告长什么样

8 个章节 / 12 张图表：

| 章节 | 内容 |
|------|------|
| 01 训练总览 | 总跑量/次数/时长/爬升，周跑量 vs 目标线，类型分布 |
| 02 焦点跑逐公里技术分解 | 每公里配速+心率，步频/步幅/垂直振幅三条技术曲线 |
| 03 心率强度与有氧效率 | Z1–Z5 占比，逐课 100% 区间堆叠柱，配速-心率散点拟合（漂移 + 目标配速预测心率） |
| 04 热适应与天气 | 每次训练 WBGT + 平均心率双轴，跨日适应信号 |
| 05 负荷与恢复 | ACWR/Form/训练状态，HRV·RHR，睡眠时长·分数，Body Battery·压力 |
| 06 身体成分与能量 | LBM/BMR/TDEE/跑步消耗/宏量目标 |
| 07 目标与成绩预测 | 佳明官方预测 + Riegel 预测双轨对照，vs 目标配速差距 |
| 08 教练建议 | 全部从真实数据推导，不写空话 |

设计系统 Ride Relief：暗黑底 `#0b0d0c` + 招牌青柠 `#d6ff64`。

## 安装 / 配置

### 1. 凭据

在 **本 skill 根目录** 创建 `.env`：

```ini
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=your_password
GARMIN_IS_CN=1            # 中国区账号（connect.garmin.cn）必填
```

> **零配置复用**：如果你已经装了 `run-coach` 或 `coros-mcp-energy-lab` skill 并配过佳明凭据，
> 这里可以直接跳过 —— 脚本会自动去那些 skill 的 `.env` 里找。

首次登录如需短信/邮箱验证码，脚本会交互式提示；token 缓存在 `.garth/`，后续免密。

### 2. 运动员档案

复制 `config.example.json` 为 `config.json` 后改（**只需改这一个文件**；
不复制也能跑，脚本会自动回落到示例配置）：

```jsonc
{
  "athlete":  { "name":"跑者","sex":"male","age":30,"height_cm":175,
                "weight_kg":null,"bodyfat_pct":null,      // null = 自动从 Garmin 拉
                "max_hr":null,"lt_hr":null,"resting_hr":null },
  "location": { "name":"北京","latitude":39.9042,"longitude":116.4074 },
  "goals":    { "race_name":"目标赛事","race_date":"2026-12-31",
                "race_distance_km":42.195,
                "target_finish":"4:30:00","target_pace_sec_per_km":384,
                "stretch_finish":"4:15:00","weekly_volume_target_km":50 },
  "activity_factor": 1.55,
  "macro": { "protein_g_per_kg":2.0, "carb_g_per_kg":5.0, "fat_g_per_kg":1.0 }
}
```

### 3. 依赖

```bash
pip install garminconnect==0.3.11 httpx requests
```

## 用法

```bash
cd ~/.workbuddy/skills/garmin-panorama-training-analysis

# 最近 14 天
python3 scripts/fetch_data.py --days 14
python3 scripts/generate_report.py

# 指定区间
python3 scripts/fetch_data.py 20260801 20260818
python3 scripts/generate_report.py data/garmin_20260801_20260818.json

# 指定焦点跑（默认是距离最长的一次跑步）
python3 scripts/generate_report.py --focus-run 12345678901
```

> Windows 上若 `python3` 不可用，用 `python` 或你的 venv 解释器全路径替换即可。

产出在 `data/`：
- `data/garmin_<start>_<end>.json` —— 标准化原始数据
- `data/report_<start>_<end>.html` —— 报告（双击打开，离线可用）

### 加速选项

区间很长时逐日体征和逐活动明细会拖慢拉取：

| 选项 | 效果 |
|------|------|
| `--no-detail` | 跳过 splits / HR 分区 / 天气（最快，只出总览） |
| `--no-daily` | 跳过 HRV / 睡眠 / Body Battery |

先 `--no-daily --no-detail` 快速确认活动清单，再全量跑。

## 质量门禁

`generate_report.py` 结尾自动执行两道检查，**失败会明确报错**：

1. **JS 语法检查** —— 用 Node 逐个解析内联 `<script>`
2. **图表数据完整性** —— data 非空非全 0、labels 与 data 长度一致、日期格式 MM/DD

通过时会打印：
```
=== 质量门禁 PASS ===
  内联脚本 2 段 · 图表数据 7 组 · 输出 .../data/report_20260901_20260915.html
```

## 目录结构

```
garmin-panorama-training-analysis/
├── SKILL.md                       给 Agent 读的（API 参考 / 铁律 / 踩坑）
├── README.md                      本文件
├── LICENSE                        MIT
├── config.example.json            配置模板（入库）
├── config.json                    个人档案（**不入库**）
├── .env                           凭据（**不入库**）
├── .garth/                        token 缓存（**不入库**）
├── scripts/
│   ├── garmin_client.py           凭据解析 + 登录 + 限流重试
│   ├── fetch_data.py              拉数 → 标准化 JSON
│   └── generate_report.py         JSON → HTML + 质量门禁
├── references/
│   ├── heat_metrics.py            露点/湿球/体感/酷热指数/估算WBGT
│   ├── load_balance.py            ACWR / Form / 就绪度 / Riegel
│   ├── body_composition.py        LBM / BMR / TDEE / 宏量
│   └── ride_relief_style.css      设计系统
├── assets/chart.umd.min.js        Chart.js 4.4.4（离线内联）
└── data/                          产出（**不入库**，含 GPS 与个人体征）
```

`references/` 下三个 Python 模块都带 `__main__` 自测，改完直接跑：
```bash
python3 references/load_balance.py
```

## 实测样例

2026-09-01 ~ 09-15（15 天 / 8 次跑 / 63.9 km）：

- 焦点跑：09-13「济南市 - 恢复」22.502 km / 2:40:55 / 平均 HR 151 / 23 段逐公里
- 运动员：体重 76.0 kg / LT 172 bpm / LT 功率 338 W / VO2max 46.1（由训练状态回填）
- 训练状态：ACWR 1.0（平衡），急性 = 慢性 = 678，`AEROBIC_LOW_SHORTAGE`（低强度有氧不足）
- 成绩预测：佳明官方全马 **4:48:45**（6:50/km），比 Riegel 快 25 分，比 5:00 目标快 11 分
- 心率分区：Z1 96-114 / Z2 115-133 / Z3 134-153 / Z4 154-172 / Z5 173+（活动推断）
- 天气：Open-Meteo 26.0 °C / RH 48% / 风 11.3 km/h，手表实测 22.8 °C
- 每日体征：HRV 46–65 ms、RHR 46–50 bpm、睡眠 7.27–8.33 h、睡眠分数 84–97

## 已知限制

- `get_heart_rate_zones()` 对部分账号返回空壳，会自动从活动明细推断分区
  （输出里标记 `hr_zone_source: "activity-inferred"`）
- Garmin 体脂秤无记录时，`bodyfat_pct` 回落到 `config.json` 的估算值
- `get_max_metrics` 对部分账号返回 `[]`，VO2max 会自动从训练状态回填（标记 `vo2max_source`）
- 耐力分数 / 爬坡分数对多数账号本就无数据（`enduranceScoreDTO: null`），显示为「—」属正常
- 佳明官方成绩预测缺少某一项时（如没有全马预测），第 07 章只显示 Riegel 一栏
- 天气主用 Open-Meteo 历史归档（按 GPS 坐标 + 开始时刻），室内跑步（跑步机）无 GPS 时回落到 `location` 坐标
- 429 限流已内置 2s/6s/18s 退避重试，但超长区间仍建议分段拉

## 与 COROS 版的主要差异

| 维度 | COROS 版 | Garmin 版 |
|------|----------|-----------|
| 逐公里 | `activity/detail/query` | `get_activity_splits → lapDTOs` |
| HR 分区 | `frequencyList` 6 区 | `get_activity_hr_in_timezones` 5 区（`secsInZone`） |
| 负荷 | ATI / CTI | `dailyTrainingLoadAcute/Chronic`（自算 ACWR） |
| 天气 | Open-Meteo | Open-Meteo + 手表实测（需英制转公制） |
| 恢复 | COROS 自有 | HRV / RHR / 睡眠分期 / Body Battery / 压力 / 训练就绪度 |
| 特色 | — | 成绩预测、乳酸阈功率、耐力/爬坡分数、力量组次 |
