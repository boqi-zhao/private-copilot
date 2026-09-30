---
name: expense-capture
description: 记录消费。支持两种录入方式：用户直接打字描述（如"午饭35支付宝"），或发来消费截图（支付宝/微信支付成功页、银行账单、订单页）。提取字段后直接写入，回复里带上记录 id；用户若要纠正，回复"分类改成交通"这类话即可修改刚记的这笔。也负责回答消费类问数（如"10月1日到7日花了多少"）。注意：体检/化验单走 health-report；财务总览（各账户余额）走 finance-snapshot。
---

# 消费记录

> ## 核心规则
>
> **1. 直接写入，不要问"要不要写入"。** 打字和截图都是即时记账，用户发来就是要记。
> **2. 回复必须带 `id`。** 形如 `已记账 ✅ (id=21)`，用户靠这个 id 纠正。
> **3. 不要用 `ask_user` 确认。** 飞书没有原生按钮，且它的等待会和飞书
>    300 秒队列上限冲突，导致用户回复被卡住几分钟。只有**歧义消解**（见文末）才用。

## 两种录入方式（都要支持）

| 方式 | 用户做什么 | 你做什么 |
|---|---|---|
| **打字** | "午饭35 支付宝"、"打车45微信" | 从文字里提取字段 |
| **截图** | 发来支付成功页截图 | 从图里提取字段 |

**录入方式记在 `source` 字段**：打字填 `text`，截图填 `image`。

### 打字录入时的注意点

- 用户通常只说金额 + 一句描述，**时间默认用当前时间**（这是唯一允许默认时间的场景，因为用户是即时记账）。如果用户说了别的时间（"昨天晚上"），用他说的时间。
- 平台没说的话：**不要猜**，填 `其他`，并在回复里问一句。
- 分类从描述里推断，不要额外追问（"午饭"→餐饮/日常）。

### 截图录入时的注意点

- **金额取实付/优惠后金额**，不是原价。页面上有两个数时取实付。
- **时间取交易时间**，不是截图时间。
- 多个候选金额、金额模糊、有小数点歧义时，**选最可能的那个先记下来**，
  并在回复里说明"我按 ¥x 记的，如果不对回我一句就改"——用 id 纠正比打断流程更省事。
- 银行账单截图常只有商户代码，原样记进 `raw_desc` 即可。

## 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `occurred_at` | ✅ | 消费时间，`YYYY-MM-DD HH:MM` |
| `category_l1` | ✅ | 一级分类，见下方受控词表 |
| `category_l2` | | 二级分类，自由填，可空 |
| `amount` | ✅ | 金额，实付 |
| `platform` | ✅ | 支付宝 / 微信 / 现金 / 信用卡 / 其他 |
| `source` | ✅ | `text` 或 `image` |
| `raw_desc` | | 用户原话或截图摘要 |

## 一级分类（受控，不得自创）

```
餐饮   吃饭、外卖、买菜        → 二级常用：日常 社交 外卖 买菜 咖啡饮品
交通   通勤与出行              → 二级常用：地铁公交 打车 加油 停车 高速 火车 机票
购物   买东西                  → 二级常用：日用 数码 服饰 美妆 家居
居住   住相关固定支出          → 二级常用：房租 房贷 水电燃气 物业 宽带
医疗   看病、买药、体检        → 二级常用：门诊 药品 体检 牙科 保险
娱乐   休闲娱乐与订阅          → 二级常用：电影 游戏 演出 会员订阅 旅游
学习   教育、书籍、课程        → 二级常用：书籍 网课 培训 考试
人情往来 红包、礼金、赠送      → 二级常用：红包 份子钱 礼物 孝敬父母
健身   运动相关支出            → 二级常用：健身房 私教 装备 赛事
其他   兜底
```

二级分类**可以自由发挥**，上面只是常用参考。想不到合适的就留空。

### 判断口径

- 有他人参与、金额明显偏高 → `餐饮` + 二级 `社交`
- 药店买药 → `医疗`（不是购物）
- 私教课、运动装备 → `健身`
- 视频/音乐/云盘会员 → `娱乐` + 二级 `会员订阅`
- 归入 `其他` 时必须说明理由

## 写入：直接写，别问

**提取完字段就写入，不要弹确认。** 打字录入是即时场景，用户发来就是要记。

```bash
python3 ~/project/private-copilot/scripts/validate.py expense --json '{
  "occurred_at": "2026-10-01 12:30",
  "category_l1": "餐饮",
  "category_l2": "日常",
  "amount": 38.5,
  "platform": "支付宝",
  "source": "text",
  "raw_desc": "午饭"
}'
```

**不要直接写 SQL。** 脚本负责分类白名单校验和重复检测。

返回里带 `id`：

```json
{"ok": true, "id": 21, "detail": {...}}
```

### 回复必须带 id

```
已记账 ✅ (id=21)
金额：¥38.50
分类：餐饮 / 日常
平台：支付宝
时间：2026-10-01 12:30
```

**`id` 一定要写**——用户靠它纠正。分类回退到「其他」或平台没识别时，额外说明一句：

- 分类回退 → "未能确定分类，暂记为其他"
- 平台未识别 → "未识别到支付平台，暂记为其他"

### 为什么不用 ask_user 确认

三个原因，都有实测依据：

1. **飞书没有原生按钮。** 只有 Telegram、Discord、Slack、Mattermost 等支持；
   飞书上会降级成一坨重复的文本选项加一个 `Other...`，很难看。
2. **`ask_user` 的等待和飞书队列上限冲突。** 飞书对同一会话有 300 秒上限：

   ```
   11:48:13  ask_user 开始等待
   11:53:12  per-chat task exceeded 300000ms cap; evicting from queue
   11:53:14  question.waitAnswer 295860ms
   ```

   用户秒回的「写入」被卡了将近 5 分钟才处理。
3. **`ask_user` 只能改当前这一笔，且必须当场改。** 用户过一会儿才发现分类错了，
   或者想改上一笔，它就无能为力。

## 纠正：用户回复修改要求

用户看到 `id` 后，可能回一句：

- "分类改成交通"
- "金额应该是 45，不是 42"
- "这笔是微信付的"
- "id=20 那个时间不对，改成昨天中午"

**这时用 `expense-update`，不要直接写 SQL。**

```bash
# 改最近一笔（不带 id）
python3 ~/project/private-copilot/scripts/validate.py expense-update --json '{
  "category_l1": "交通",
  "category_l2": "打车"
}'

# 指定 id
python3 ~/project/private-copilot/scripts/validate.py expense-update --json '{
  "id": 20,
  "amount": 45
}'
```

可改字段：`occurred_at`、`category_l1`、`category_l2`、`amount`、`platform`、`raw_desc`、`note`。
**只传要改的字段**，没传的保持原值。

### 判断改哪一笔

| 用户怎么说 | 你传什么 |
|---|---|
| "分类改成交通"（刚记完） | 不传 `id` → 改最近一笔 |
| "id=20 改成交通" | `"id": 20` |
| "上一笔" | 不传 `id` |

**先 `expense-last` 看一眼再改**，确认目标对不对：

```bash
python3 ~/project/private-copilot/scripts/validate.py expense-last
```

返回最近 5 笔，含 `id`、时间、分类、金额、平台。

### 超过 24 小时的记录

如果要改的这笔 **`occurred_at` 距今超过 24 小时**，
**先用 `ask_user` 确认一次**（这是唯一该用它的场景，见文末），再执行。

### 返回码

| 返回 | 含义 | 你回复 |
|---|---|---|
| `ok: true` + `detail.changes` | 改成功 | 说明改了什么（from → to） |
| `ok: true` + `detail.noop` | 没有实际改动 | "这笔本来就是 xx，无需修改" |
| `reason: "invalid"`（码 1） | 校验失败 | 看 `error` 字段，说明哪里不对 |
| `reason: "duplicate"`（码 2） | 改完会与别笔重复 | 告知 `existing_id`，别自动重试 |
| `reason: "ambiguous"`（码 4） | 无法确定改哪笔 | 列出候选让用户挑 |

**`invalid` 不会静默回退。** 与新增不同：新增时非法分类会回退到「其他」，
修改时**直接拒绝**——用户是明确指定要改成什么，回退会违背他的意图。

回复示例：

```
已修改 ✅ (id=20)
分类：餐饮 → 交通
```

## 只有一种情况用 ask_user：歧义消解

**当要改的记录无法唯一确定时**（如"改一下上周那笔咖啡"但有多笔匹配），
才用 `ask_user` 让用户在候选里挑：

```
ask_user({
  questions: [{
    id: "pick_expense",
    header: "改哪一笔",
    question: "找到多笔候选，要改哪一笔？\n\n1. id=18 09-28 ¥32 微信 咖啡\n2. id=15 09-27 ¥28 支付宝 咖啡",
    options: [
      { label: "id=18 ¥32 微信 09-28" },
      { label: "id=15 ¥28 支付宝 09-27" }
    ]
  }],
  timeoutSeconds: 240
})
```

**`timeoutSeconds` 最大设 240**——必须小于飞书的 300 秒队列上限，
否则用户回答又会被卡住。

这种场景很少见（大多数纠正是针对刚记的那笔），不会频繁触发队列冲突。

### 旧退出码（新增时）

| 0 | 成功 | 回复金额/分类/平台/时间 |
| 1 | 校验失败 | 看 `error` 字段 → 追问用户 |
| 2 | 重复 | 回复"这笔似乎已记过（id=N），还要再记吗？"，**不要自动重试** |

`detail.category_fallback == true` 时说明分类被回退到"其他"，要告知用户。

## 回复格式

写入成功后：

```
已记账 ✅ (id=21)
金额：¥38.50
分类：餐饮 / 日常
平台：支付宝
时间：2026-10-01 12:30
```

修改成功后：

```
已修改 ✅ (id=21)
分类：餐饮 → 交通
```

---

# 消费问数

用户问花销相关问题时，**用 `query.py` 而不是自己拼 SQL**。日期边界、聚合口径都已在脚本里处理。

## 常用查询

```bash
# 区间总额 + 一级分类分布（最常用）
python3 ~/project/private-copilot/scripts/query.py spend --from 2026-10-01 --to 2026-10-07

# 按平台
python3 ~/project/private-copilot/scripts/query.py spend --from 2026-10-01 --to 2026-10-07 --by platform

# 按天（看趋势）
python3 ~/project/private-copilot/scripts/query.py spend --from 2026-10-01 --to 2026-10-07 --by day

# 按二级分类
python3 ~/project/private-copilot/scripts/query.py spend --from 2026-10-01 --to 2026-10-07 --by category_l2

# 明细（用户想看具体是哪些）
python3 ~/project/private-copilot/scripts/query.py list --from 2026-10-01 --to 2026-10-07 --limit 20
```

**日期区间含首尾两天**（`--from 10-01 --to 10-07` 包含 10-07 全天）。

## 时间表达转换

| 用户说 | 参数 |
|---|---|
| 10月1日到10月7日 | `--from 2026-10-01 --to 2026-10-07` |
| 这个月 / 本月 | `--from <本月1日> --to <今天>` |
| 上周 | 按周一至周日换算 |
| 最近7天 | `--from <今天-6天> --to <今天>` |
| 昨天 | `--from <昨天> --to <昨天>` |
| 不提时间 | 不传参数，默认本月 |

**当前年份以系统时间为准**，用户只说"10月1日"时用今年。

## 兜底：自由 SQL

预设查询覆盖不了时（比如"哪家店我花得最多"），用只读 SQL：

```bash
python3 ~/project/private-copilot/scripts/query.py sql --q \
  "select IFNULL(raw_desc,'(无描述)') d, count(*) n, round(sum(amount),2) v
   from expenses group by d order by v desc limit 10"
```

只读、单条、禁 DDL/DML。表结构见 `scripts/schema.sql`。

## 回复问数时的注意点

- **先给结论，再给拆解**。不要一上来倒 JSON。
- 金额保留两位小数，用 `¥` 前缀。
- 分布类问题给出占比，让用户有直观感受。
- **不要编造数据**。查询结果为空就说"这段时间没有记录"。
- 如果结果里 `category_l1` 有"其他"且占比高，可以提醒一句"有些记录分类待细化"。

---

# 图表（用户要"看图"时）

用户说「画个图」「饼图」「折线图」「趋势图」「占比」「可视化」时，**不要自己写脚本画**，
直接用现成的 `chart.py`。它已经处理好了中文字体、标签防重叠、2x 高清。

> ⚠️ **不要现场造轮子。** 曾经有过一次：agent 自己去 npm 装 `@napi-rs/canvas`、
> 下载 17MB 中文字体、写了 60 行渲染代码 —— 结果写进 `/tmp`，重启就没了，
> 而且只能画饼图。**先看这个技能，用现成命令。**

## 命令

```bash
# 分类占比饼图
python3 ~/project/private-copilot/scripts/chart.py pie \
  --from 2026-10-01 --to 2026-10-07 --out /tmp/chart.png

# 柱状图（by: category_l1 | platform | day）
python3 ~/project/private-copilot/scripts/chart.py bar \
  --from 2026-10-01 --to 2026-10-31 --by platform --out /tmp/chart.png

# 折线图（metric: day | month）
python3 ~/project/private-copilot/scripts/chart.py line \
  --from 2026-10-01 --to 2026-10-31 --metric day --out /tmp/chart.png

# 资产/负债/净资产三线对比
python3 ~/project/private-copilot/scripts/chart.py finance --out /tmp/chart.png

# 全部历史月度趋势
python3 ~/project/private-copilot/scripts/chart.py trend --out /tmp/chart.png
```

## 怎么选图

| 用户想问 | 用哪个 |
|---|---|
| 钱花在哪了 / 分类占比 | `pie` |
| 各平台花了多少 / 每天花多少 | `bar` |
| 最近花费走势 / 哪天花得多 | `line --metric day` |
| 月度对比 | `line --metric month` 或 `trend` |
| 资产/负债/净资产变化 | `finance` |

## 发图

`chart.py` 只生成本地 PNG。**必须再用 `message` 工具发出去**，用户才看得到：

```
message(action="send", mediaUrls=["/tmp/chart.png"], text="...")
```

## 三条硬要求

1. **图 + 文字一起发。** 只发图不解释，用户还得自己看数字；只发字不发图，等于没画。
   文字里给结论（总额、最大项、占比），图负责直观。
2. **先把 JSON 里的数字读出来再组织话术。** `chart.py` 成功时会打印 JSON
   （含 `total`、`count`、`items`），用里面的真实数字，**不要凭图估算**。
3. **空数据不要画图。** 若 JSON 里 `count` 为 0 或 `items` 为空，
   直接文字回复"这段时间没有记录"，别发一张空图。

## 出问题时

| 现象 | 原因 / 处理 |
|---|---|
| 报「渲染器不存在」 | 仓库没同步，检查 `scripts/chartgen/render.js` |
| 报「渲染依赖未安装」 | 跑 `cd ~/project/private-copilot/scripts/chartgen && npm install` |
| 图上中文是方块 | 缺字体，`sudo apt install fonts-noto-cjk` |
