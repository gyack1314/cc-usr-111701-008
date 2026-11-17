# stac-search 来源契约

来源清单中的资产长度与 SHA-256 用于核对本地原文字节。文档的 collection 与 protocol 必须匹配清单，现有记录类型包括 query, item, asset, collection。每条记录以 kind、id、version 定位，refs 指向同集合的其他记录，payload 原样留存。来源版本冲突会中断接收；本地引用闭合只表示当前收到的引用能定位，不表示远端序列或授权资料已经完整。

## 已接收的协议资料
现有入口保留 STAC FeatureCollection 的 next 请求、collection/id、datetime 与资产地址。它不证明分页期间搜索全集稳定，也不下载影像字节。

同集合第二次来源接收保存在 data/conflict-case.json。冲突事务必须保持第一份收据和旧投影。

## 收割账本与交付单（harvest_store）

收割层与上面的来源账本相互独立，全部表只追加（harvest_runs / harvest_pages / item_versions / item_observations / item_absences / item_assets / deliveries）：

- item 身份是 `collection + item_id`，内容版本是整条 Feature 规范化 SHA-256；同一内容版本重复出现只在 item_observations 追加页码，不写新版本。
- 资产地址与元信息按 `(collection,item_id,content_version,asset_key)` 与同版 item 绑定；Feature 修订（含资产地址变化）追加新版本，旧版本与旧资产绑定原样保留。
- item_absences 只记录账本中曾出现的 id：覆盖已证明的收割中缺席记 `deleted` 并附最后出现证据；覆盖未证明记 `absent_unknown`，证据里携带当时的 coverage 结论，禁止把暂时未出现误判为删除。
- deliveries 永不更新：同一 harvest 重复交付返回同一 delivery_id；新收割产生新交付单，历史清单（含冻结查询、服务身份、页摘要、场景内容版本与资产）保持不变，不能用当前目录覆盖历史订单。
- 覆盖结论只有 `proven-snapshot`、`proven-overlap` 与 `unknown` 三态；unknown 必须带 gaps 原因。覆盖未证明、分页未终止、存在未消解修订或资产缺绑定时，交付被拒绝而不是降级产出。

