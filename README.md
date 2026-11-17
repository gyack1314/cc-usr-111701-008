# STAC 搜索结果的游标漂移与资产证据采集资料服务

项目接收 stac-search 来源资料，现有目录只核对 query, item, asset, collection 的基本身份和引用。`capture_data.io` 冻结字节和请求来源，`capture_data.domain` 核对本领域记录结构，`capture_data.journal` 封存来源原文和版本，`capture_data.projection` 显示本地引用缺口。本地判断只覆盖本地资料，不推断外部服务的连续性或全量完整性。

## 可审计搜索收割（harvest）

`capture_data.harvest` 与 `capture_data.harvest_store` 在原接收链路之外新增收割层，解决服务新增影像后翻页顺序变化（末页 item 跨页重复、旧 item 被跳过）的问题。引擎不直接联网：传输层以参数注入，离线审计与测试使用冻结请求/响应转录的 `ReplayTransport`。

收割收据（receipt）逐条留痕，可审计且可 JSON 序列化：

- **冻结查询**：方法、URL、请求头、POST body、采集窗口整体规范化哈希为 `query_digest`；凭据请求头只记录脱敏值摘要。
- **服务标识与响应顺序**：每页记录请求语义、响应状态/头、body 摘要、快照标识、页码与 next 审计；服务的 server、STAC 版本、conformance、快照稳定性聚合在 `service`。
- **内容版本去重**：item 以 `collection/id` 定位，内容版本是整条 Feature 规范化后的 SHA-256。同版重复出现只追加页码（`duplicate_occurrences`），不新增版本；Feature 任何字段变化（含资产地址）产生新版本并标记 `mid_run_revision`。
- **缺席三分法**（跨收割，由账本判定）：仍出现＝在册；曾出现且本次在**已证明覆盖**的收割中缺席＝`deleted`（附最后出现证据）；覆盖未证明时缺席＝`absent_unknown`，绝不断言删除。
- **续页语义**：`follow_next` 保留 GET/POST、POST body 与 `merge`、链接自带 headers；POST 续页未显式给 body 时携带原查询 body。next 指向其他主机（或 scheme/端口变化）即跨凭据域，自动剥离 `Authorization`、`Cookie`、`X-Api-Key` 及计划声明的自定义凭据头，并在审计中记录剥离事实。非 http(s) 续页直接拒绝。
- **覆盖证明**：全程稳定快照标识且服务声明快照 conformance → `proven-snapshot`；或两次自然终止的收割在重叠窗口内 `(collection,id,内容版本)` 完全一致 → `proven-overlap`。证明不了时 `coverage.state=unknown` 并给出具体 `gaps`（分页未终止、快照中途变化、窗口不一致等），不伪装完整。
- **交付门控**：只有覆盖已证明、分页自然终止、无收割期/跨收割未消解修订、每个内容版本都绑定资产地址与元信息时，`build_delivery` 才产出交付单。交付单只追加、永不更新，历史订单不被后续收割的当前目录覆盖；清单中的资产与具体 `content_version` 绑定，配合冻结查询可完整复现。

## 命令

Python 3.11 以上，无第三方依赖。样例均为合成，不访问真实站点。

```sh
python -m unittest discover -s tests -v
python -m compileall -q capture_data tests
python -m capture_data.cli inspect data/normal.json

# 按冻结计划重放收割（计划与转录见 data/harvest-demo/，由 tools/build_harvest_fixtures.py 生成）
python -m capture_data.cli harvest data/harvest-demo/plan.json \
  --db /tmp/harvest.sqlite3
# 叠加重叠窗口印证，满足门控时同时交付
python -m capture_data.cli harvest data/harvest-demo/plan.json \
  --db /tmp/harvest.sqlite3 \
  --overlap-transcript transcript-b.json \
  --overlap-window overlap-window.json --deliver
# 对已提交收割单独生成不可变交付单
python -m capture_data.cli deliver --db /tmp/harvest.sqlite3 \
  --harvest-id <harvest_id>
```

Dockerfile 提供 Linux 运行环境；`docker build -t source-capture .` 构建，`docker run --rm source-capture python -m unittest discover -s tests -v` 运行测试。

协议样例可通过 `python -m capture_data.cli inspect data/protocol-case.json` 查看。该命令只说明现有接收能力，不证明远端连续性。
