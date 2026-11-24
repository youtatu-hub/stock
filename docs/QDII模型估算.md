# QDII 模型估算（premium_api_version: 5）

五只海外 ETF 在折溢价参考面板新增独立的“模型估算”区块。513500 使用 ES，513300、513100、159501、159941 使用 NQ；汇率代理统一为 USD/CNH 即期买卖中值。腾讯 IOPV 参考和已披露净值继续独立展示。本功能不依赖 RQData，也不关闭第 19 项。

## 计算口径

```text
估算净值 = 实际最新已披露净值
         × (期货现价 / 该净值日期美股收盘时的期货基准价)
         × (USD/CNH现价 / 同一收盘时点的USD/CNH基准价)

估算折溢价 = (有效ETF市价 / 估算净值 - 1) × 100%
```

例如最新净值仍属于周五，周二计算时分母仍使用周五的基准，保留周一以来的全部价格变化。不能用报价自带的日涨跌幅、昨日结算价或另一个日期的基准代替。

- 基准按净值记录中的实际日期精确匹配，不假设净值一定是 T-1。新净值出现但对应基准未采到时，明确不可用，不退回旧净值。
- NQ/ES 使用新浪连续合约最新价；USD/CNH 使用同一接口的 `(买价 + 卖价) / 2`。这不是央行中间价，也不是 USD/CNY。手动基准必须采用相同品种和价格口径。
- 当前期货、汇率均需要带日期的来源时间，不能在未来，距当前不得超过 120 秒。来源失败不使用旧成功结果兜底；缓存 15 秒，读取后仍检查原始报价时间。
- 市价缺失或过期时，可以保留有效模型净值，但估算折溢价为空。模型本身缺少期货、汇率或基准时，`est_nav`、`est_nav_time`、`est_premium_pct` 均为空。
- 达到 5% 偏离只作页面信息提示，`alert_eligible` 始终为 `false`。不启用原有 IOPV 的 3% 实时提醒。
- 模型未校准误差。不承诺 ±0.3%～0.5% 精度，也不宣称一定优于官方 IOPV；期货基差、连续合约换月、离岸/在岸汇率差异、现金仓位和费用都可能影响结果。

## 收盘基准自动采集

`app.py` 启动时同时启动收盘采集线程，无需另设固定北京时间 cron。服务必须在收盘窗口运行。

- 以 `America/New_York` 的正常 16:00 收盘为准：北京时间冬令时次日 05:00、夏令时次日 04:00。
- 使用已核对的 2026～2028 年交易日历，跳过周末和休市日；提前至美东 13:00 收盘的日期会相应提前采集。例如 2026-11-27 对应北京时间 11-28 02:00。
- 日历在 `data/us_stock_sessions.json`。临时休市、收盘安排调整或年度超出范围时，需要依据交易所公告维护，并重启服务加载；超出已配置年度会报错，不猜测收盘时间。
- 收盘后 60 秒内尝试采集。三个输入的各自报价时间必须位于收盘前后 30 秒内，并保留真实报价时间和获取时间。这是“收盘附近采样”，不伪称精确成交于收盘一瞬间。
- 一次原子保存 NQ、ES、USD/CNH；任一缺失、陈旧或时间不符则整组不保存并记录错误。在窗口内重试，错过窗口后不拿现价补历史。
- 记录按美股交易日期保存。例如北京时间 11-21 05:00 采集的基准属于 `2026-11-20`。
- 已有该日期基准时不重复抓取、不覆盖。服务重启后仍读取相同记录。

默认数据库为 `runtime/qdii/baselines.sqlite3`，使用 SQLite 参数化读写。`runtime/` 已从 Git 和 Docker 构建上下文排除；不要提交真实报价或基准记录。

## 首次基准录入

当前没有预填任何真实历史基准。首次使用需要录入与各基金实际最新净值日期对应的 NQ、ES 和 USD/CNH 收盘时点价格。若暂时没有这些数值，页面会显示“估算不可用／缺少净值日……的收盘基准”；服务从下一次收盘起积累数据，后续净值日期与已采集基准匹配后自动可用。

以下日期仅为文档示例，应先查看 `/api/etf-premium?symbol=513300` 中的 `model_estimate.nav_date`，逐个补齐实际需要的日期。**不能把期货日线结算价、当前价或时间未经确认的历史价格填作基准。** 模板中价格为 `null`、来源为空，未填写就无法导入。

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Path runtime/qdii-imports -Force | Out-Null
& 'D:\Miniconda3\envs\stock\python.exe' tools/qdii_baselines.py status
& 'D:\Miniconda3\envs\stock\python.exe' tools/qdii_baselines.py template --date 2026-11-20 |
    Set-Content -LiteralPath runtime/qdii-imports/2026-11-20.json -Encoding utf8

# 填写 source、三个 price，并按真实记录核对三个 time 后导入。
& 'D:\Miniconda3\envs\stock\python.exe' tools/qdii_baselines.py import --file runtime/qdii-imports/2026-11-20.json
```

在其他 Python 环境运行时，替换解释器路径即可。若提示缺少纽约时区数据，在该环境安装 `tzdata`；Docker 镜像已包含系统时区数据。

Docker / Ubuntu：

```bash
docker compose up -d --build
docker compose exec -T stock-analysis python tools/qdii_baselines.py status
mkdir -p runtime/qdii-imports
docker compose exec -T stock-analysis python tools/qdii_baselines.py template --date 2026-11-20 > runtime/qdii-imports/2026-11-20.json

# 填好并核实价格、时间和来源后：
docker compose exec -T stock-analysis python tools/qdii_baselines.py import --file - < runtime/qdii-imports/2026-11-20.json
```

普通导入不覆盖已有同日记录；需要更正时显式添加 `--replace`。`capture` 子命令仅允许在当日收盘采集窗口手动触发，无法回补过去日期。

Compose 使用命名卷 `qdii-baselines` 保存 `/app/runtime/qdii`。重建镜像和普通重启保留数据；删除命名卷会丢失基准，需另行备份。直接运行 Python 时，可用进程环境变量 `STOCK_ESTIMATE_DIR` 指定数据库目录。

## 接口与 AI

`GET /api/etf-premium` 新增独立 `model_estimate` 对象：

- `kind: model_estimate`、`label: 模型估算`、`status`、`reason_code` 和 `note`。
- `est_nav`、`est_nav_time`、`est_premium_pct`；与现有 `iopv` 字段不混用。
- `nav_date`、`base_nav`、`baseline_close_time`、`baseline_source`、`baseline_origin`。
- `inputs.future`、`inputs.fx`、`inputs.base_future`、`inputs.base_fx`：分别保留价格、时间、品种、价格口径及来源；自动采集还保留获取时间。
- `est_nav_time` 是本次期货与汇率输入中较早的报价时间；`calculated_at` 是本站计算时间，两者不互相替代。
- `fresh_until` 到期后页面取消当前估算展示；`notice_threshold_pct: 5` 只用于信息提示。

AI 上下文保留明确标注的模型估算，系统提示要求引用“模型估算”及输入时点，不得改称 IOPV、不作固定精度或确定套利承诺。来源参考 IOPV 对象仍按原规则移除。

`/api/health` 的 `premium_api_version: 5` 仅证明新版代码已加载。真实数据能否估算，以 `model_estimate.status` 为准；第 19 项仍未完成。

## 已核验来源

- [新浪 NQ/ES/USD-CNH 报价](https://hq.sinajs.cn/list=hf_NQ,hf_ES,fx_susdcnh)：核对品种、价格、日期和时间，未保存实际返回行情到仓库。
- [AKShare 新浪外盘期货适配](https://github.com/akfamily/akshare/blob/main/akshare/futures/futures_hq_sina.py)：核对最新价与昨日结算价字段区别。
- [NYSE 交易时间和休市日历](https://www.nyse.com/markets/hours-calendars)：核对正常收盘、2026～2028 年休市及提前收盘安排。

来源核对日期：2026-11-24。源码与合成测试不代表模型误差已经校准。

## 验证记录

- 后端覆盖跨周末基准匹配、汇率方向、缺失与过期输入、停用旧估算、SQLite持久化、采集窗口、夏令时及提前收盘、API/AI隔离；前端覆盖模型标签、各输入时点、5%信息提示和到期失效。
- 真实接口核验：NQ、ES及USD/CNH均返回带日期时间的报价；五只ETF当前净值日期均为2026-11-20，因未录入真实基准，均明确返回 `baseline_missing`，没有伪造可用估算。健康接口返回版本5。
- 新浪批量接口不解码品种之间的 `%2C`，因此使用固定品种白名单构造保留逗号的已核验路径，已补充回归测试。
- [页面截图](qdii-model-estimate.png)使用合成数据，展示可用与缺基准两种状态；未调用真实AI，未把真实市场报价写入仓库。
- 本机没有Docker，未执行镜像构建。自动收盘采集的时间与写入规则已用确定性测试验证，仍需部署服务在真实收盘窗口运行，才能获得第一条真实自动采集记录。
