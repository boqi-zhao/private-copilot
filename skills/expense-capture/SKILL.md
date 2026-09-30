---
name: expense-capture
description: 从消费截图（支付宝/微信支付成功页、银行或信用卡账单、购物订单页）提取金额、商户、时间并分类记入账本数据库。适用于任何"花了钱"的凭证。注意区分：若用户想记录"吃了什么"用 meal-log；想记录"体检指标/化验单"用 health-report；想记录"运动数据"用 workout-log。医院缴费单归本技能记账（健康指标另走 health-report）。
---

# 消费截图记账

把一张消费截图变成一条结构化账本记录。

## 数据落地位置

- 数据库：`~/project/private-copilot/data/copilot.db`
- 原图：`~/project/private-copilot/data/receipts/`（由脚本自动落盘）
- 写入脚本：`~/project/private-copilot/scripts/validate.py`
- 分类词表：`~/project/private-copilot/config/categories.yaml`

**不要直接写 SQL。** 一律通过 `validate.py` 写入，它负责分类白名单校验、图片落盘与去重、业务去重。

## 分类词表（只能从中选择，不得自创）

```
餐饮-日常   自己日常吃饭、外卖、买菜做饭
餐饮-社交   与他人一起、金额明显偏高的就餐
交通        地铁、公交、打车、加油、停车、高速、火车票、机票
购物-日用   纸巾、洗护、清洁用品、衣物鞋帽
购物-数码   手机、电脑、耳机、配件
医疗        挂号、门诊、药店、体检费、牙科
居住        房租、房贷、水电燃气、物业、宽带
娱乐        电影、游戏、演出、会员订阅、旅游门票
学习        买书、网课、培训、考试报名
人情往来    红包、份子钱、礼物、孝敬父母
健身        健身房、私教课、运动装备、赛事报名
其他        无法归入以上任何一类时的兜底
```

### 判断口径

- **有他人参与、金额明显偏高**的餐饮 → `餐饮-社交`；一人日常吃饭 → `餐饮-日常`
- **药店购药、挂号、体检费** → `医疗`（不是 `购物-日用`）
- **私教课、运动装备** → `健身`（不是 `娱乐`/`购物-数码`）
- **会员订阅**（视频、音乐、云盘）→ `娱乐`
- 归入 `其他` 时，**必须在回复里说明理由**

## 提取规则

从截图中按以下优先级提取。**看不清就留空并追问，不要猜。**

| 字段 | 取什么 | 注意 |
|---|---|---|
| `amount` | **实付/优惠后金额** | 页面上常有"原价"和"实付"两个数，取实付。有多个候选金额时全部列出让用户确认 |
| `occurred_at` | **交易时间**，不是截图时间 | 截图可能是事后补发。时间缺失必须问用户，脚本会拒绝无时间写入 |
| `merchant` | 对方名称/店铺名 | 银行账单常只有商户代码，**原样保留**并在回复中提示 |
| `pay_method` | 支付宝 / 微信 / 银行卡 / 其他 | 从界面特征判断 |
| `direction` | `expense`（默认）/ `income` / `transfer` | 收入才用 income；转账用 transfer |

**金额读取是最高风险项**，以下情况必须向用户确认后再写入：

- 页面同时出现原价与实付，且差额明显
- 金额数字模糊、被遮挡、有小数点歧义
- 列表页包含多笔金额（如账单列表截图）
- 出现负数或"退款"字样

## 执行步骤

1. **提取**：读图，按上表提取字段。
2. **消歧**：如果这张图同时可能是饮食记录（如外卖订单）或运动记录，先问用户一句要记哪个，不要自行假设。
3. **确认**：把提取结果用一句话回报给用户，**金额和时间必须回报**。若用户没纠正，视为确认。
4. **写入**：
   ```bash
   python3 ~/project/private-copilot/scripts/validate.py expense --json '{
     "amount": 38.5,
     "merchant": "星巴克",
     "category": "餐饮-日常",
     "occurred_at": "2026-09-30 12:30",
     "pay_method": "支付宝",
     "image_path": "<截图的本地路径>",
     "raw_desc": "<用户的原始描述>",
     "confidence": 0.9
   }'
   ```
5. **解读返回码**：

   | 退出码 | 含义 | 你要做什么 |
   |---|---|---|
   | 0 | 写入成功 | 回复用户：金额、分类、时间 |
   | 1 | 校验失败 | 看 `error` 字段，通常是时间缺失或金额无法解析 → **追问用户** |
   | 2 | 疑似重复 | 回复"这笔似乎已记录过（id=N），还要再记一次吗？"，**不要自动重试** |

6. **分类兜底**：如果返回的 `detail.category_fallback == true`，说明你选的分类不合法已被回退到 `其他`。回复用户时说明这一点，并让他确认分类。

## 图片处理

- 图片路径来自会话附件。**截图通常是临时文件，必须通过 `image_path` 参数传给脚本**，脚本会复制到 `data/receipts/<年>/<月>/` 并按 sha256 去重。
- 不要自己下载图片、不要往数据库塞 base64 或 BLOB。
- 同一张图重复提交时，脚本会复用已存在的 `media` 记录，不会重复占空间。

## 去重规则（重要）

脚本内置两层去重：

1. **图片层**：按 sha256。同一张图再发一次不会重复存储。
2. **业务层**：`金额 + 商户 + 分钟级时间`。同一笔钱用不同截图提交两次会被拦下（退出码 2）。

被拦下时**不要改写金额或时间去绕过去**，而是问用户。

## 回复格式

简洁，包含关键信息：

```
已记账 ✅
金额：¥38.50
分类：餐饮-日常
商户：星巴克
时间：2026-09-30 12:30
```

分类回退或多候选金额时，附加一行说明。

## 常用查询

用户问"这个月花了多少""餐饮花了多少"时，直接查库：

```bash
sqlite3 ~/project/private-copilot/data/copilot.db \
  "SELECT category, ROUND(SUM(amount),2) AS total, COUNT(*) AS n
   FROM transactions
   WHERE strftime('%Y-%m', occurred_at) = strftime('%Y-%m','now','localtime')
   GROUP BY category ORDER BY total DESC;"
```

查具体记录时带上 `media_id`，可以用来看原图：

```bash
sqlite3 ~/project/private-copilot/data/copilot.db \
  "SELECT t.id, t.amount, t.merchant, t.category, t.occurred_at, m.rel_path
   FROM transactions t LEFT JOIN media m ON m.id = t.media_id
   ORDER BY t.occurred_at DESC LIMIT 20;"
```
