---
name: health-report
description: 从体检报告、化验单、检验单截图或 PDF 中提取健康指标（血常规、生化、血脂、血糖、肝肾功能等），把不同报告里的同义指标名归一后写入数据库，用于跨年度趋势对比。当用户发来化验单/体检报告，或问"我的血脂这两年怎么变的""某个指标趋势"时使用。注意：医院缴费单属于支出，走 expense-capture。
---

# 体检 / 化验指标录入

把一张化验单变成可对比的时间序列。

## 核心问题：同名归一

同一个指标在不同医院、不同年份的报告里叫法完全不同：

| 报告可能写的 | 归一为 |
|---|---|
| A1c / HbA1c / 糖化血红蛋白 / 糖化HB | `hba1c` |
| 总胆固醇 / TC / CHOL | `tc` |
| 甘油三酯 / TG / 三酰甘油 | `tg` |
| 低密度脂蛋白 / LDL-C / LDL | `ldl_c` |
| 高密度脂蛋白 / HDL-C / HDL | `hdl_c` |
| 谷丙转氨酶 / ALT / GPT | `alt` |
| 谷草转氨酶 / AST / GOT | `ast` |
| 肌酐 / Cr / CREA | `creatinine` |
| 尿酸 / UA | `ua` |
| 空腹血糖 / GLU / FPG | `glu_fasting` |
| 白细胞 / WBC | `wbc` |
| 血红蛋白 / HGB / Hb | `hgb` |
| 血小板 / PLT | `plt` |
| 收缩压 / SBP | `bp_sys` |
| 舒张压 / DBP | `bp_dia` |
| 体重 / WT | `weight` |

**归一化由脚本自动完成**：脚本会查 `metric_aliases` 表，把别名映射到标准 key。你只需要：

1. 优先填标准 key（如 `hba1c`）
2. 不确定时，把**报告原文**填进 `metric_key`，脚本会尝试查别名表
3. 别名表里没有的，脚本会原样保留 —— 这是可接受的，但**之后应该补充别名表**（见下方"维护别名表"）

## 数据落地位置

- 数据库：`~/project/private-copilot/data/copilot.db`
- 写入脚本：`~/project/private-copilot/scripts/validate.py`

**不要直接写 SQL。** 通过脚本写入，它会做别名归一、参考区间判断、图片落盘。

## 提取规则

| 字段 | 说明 |
|---|---|
| `metric_key` | 标准名（优先）或报告原文 |
| `display_name` | **报告上的原文**，保留下来便于回溯 |
| `value` | 数值。**只填数字**，把单位分离出去 |
| `unit` | 单位，如 `mmol/L`、`%`、`g/L`、`10^9/L` |
| `ref_low` / `ref_high` | 参考区间。报告上常写 `3.9-6.1`，拆成两个数 |
| `measured_at` | **采样日期**，不是出报告日期 |
| `panel` | 报告批次，建议格式 `2026-09 年度体检` |
| `institution` | 医院/体检机构 |
| `source_ref` | 报告内定位，如 `第2页 血常规` |

### 关键注意点

- **单位必须提取**。同一个指标在不同医院可能用不同单位（如血糖 `mmol/L` vs `mg/dL`），单位错了数值就没有意义。
- **异常的判断交给脚本**：填好 `ref_low`/`ref_high`，脚本会自动算出 `abnormal`（1 偏高 / -1 偏低 / 0 正常）。只有当报告明确标注了箭头（↑↓）而区间缺失时，才手动传 `abnormal`。
- **一次报告有多项指标**：一张化验单通常有 10-30 项，**每一项都要单独写一条记录**，逐条调用脚本。
- **复查对比**：报告上若同时印了"本次结果"和"上次结果"，**只提取本次**，上次的数据应该已经在库里了。
- **数值读不准时不要猜**。化验单数字密集，读错一位就是数量级错误。看不清就问用户，或跳过该指标并说明。

## 执行步骤

1. **识别报告类型**：判断是血常规、生化、血脂、尿常规还是综合体检。
2. **逐项提取**：为每一项指标准备一条 JSON。
3. **逐条写入**：
   ```bash
   python3 ~/project/private-copilot/scripts/validate.py health --json '{
     "metric_key": "hba1c",
     "display_name": "糖化血红蛋白",
     "value": 6.8,
     "unit": "%",
     "ref_low": 4.0,
     "ref_high": 6.0,
     "measured_at": "2026-09-15",
     "panel": "2026-09 年度体检",
     "institution": "某医院",
     "source_ref": "第1页 生化",
     "image_path": "<截图路径>"
   }'
   ```
   重复上面这条命令，每项指标一次。

4. **汇总回复**：写入完成后，回报**导入条数**和**异常项**。

## 回复格式

```
已导入 2026-09 年度体检 ✅
共 18 项指标

异常项（3）：
- 糖化血红蛋白 6.8 % (参考 4.0-6.0) 偏高
- 甘油三酯 2.4 mmol/L (参考 0.4-1.7) 偏高
- 高密度脂蛋白 0.8 mmol/L (参考 1.0-1.6) 偏低
```

**异常项必须主动列出** —— 这是用户最关心的信息，不要等他问。

## 趋势查询

用户问"某个指标怎么变的"时，查库并**按时间排序展示**：

```bash
sqlite3 ~/project/private-copilot/data/copilot.db \
  "SELECT measured_at, value, unit, abnormal, panel
   FROM health_metrics
   WHERE metric_key = 'hba1c'
   ORDER BY measured_at;"
```

跨指标对比：

```bash
sqlite3 ~/project/private-copilot/data/copilot.db \
  "SELECT metric_key, group_concat(value || unit) AS series
   FROM health_metrics GROUP BY metric_key;"
```

### 趋势分析的注意事项

- **同一指标可能有不同单位**，对比前先确认单位一致。不一致时**不要直接比数值**，要提示用户。
- **参考区间会变**。不同医院的正常范围略有差异，判断趋势看数值本身，不要只看 `abnormal` 标记。
- **不要下医学结论**。可以指出"这项持续偏高""比上次上升了 15%"，但**不要诊断疾病或建议用药**。需要判断时提示用户咨询医生。
- 指标为空（某项某次没测）不等于 0，不要插值填补。

## 维护别名表

遇到别名表里没有的叫法时，补进去，这样下次就能自动归一并和历史数据连上：

```bash
sqlite3 ~/project/private-copilot/data/copilot.db \
  "INSERT OR REPLACE INTO metric_aliases (alias, metric_key, unit)
   VALUES ('糖化HB', 'hba1c', '%');"
```

**优先补别名，不要去改已有记录的 `metric_key`** —— 改历史数据会切断趋势序列。

## 与 expense-capture 的边界

- **化验单、体检报告** → 本技能
- **医院缴费单、挂号费、药店小票** → `expense-capture`（记 `医疗` 类）
- 一张图上同时有费用和指标（如体检套餐收据）→ 问用户要记哪边，或两边都记并说明
