# 图外审阅、签名发布与合成接收执行契约 v1

本文件为原创单机参考设计，不复用任何生产发布实现。发布者与接收器在同一可信进程/Store 数据库、不同表和方法边界；不是物理分离主机、分布式 exactly-once、电子签章或真实 MES。

## 接口

`ActionService(service: SchedulingService)`：

- `review(principal, recommendation_id, *, action_id, recommendation_hash, decision, note)`：owner+reviewer/admin，pending_review → approved/rejected。必须核对当前来源与独验，hash 防旧页面；批准不发送。
- `request_publication(principal, recommendation_id, *, action_id, recommendation_hash, confirmed)`：approved 才建唯一 `pub-<recommendation_id>` publication；冻结签名请求；不调用接收器。
- `deliver_local(principal, publication_id, *, action_id, fault=None)`：本地受信适配器递送一次。HTTP不暴露fault。分事务 intent→receiver commit→ack，内部 after_receive 可模拟接收已提交后 ACK 丢失。
- `advance_receiver(principal, publication_id, *, action_id)`：仅已确认 partial 之后推进剩余接收；不是重发发布，不允许调用者指定成功状态或操作ID。一次全部剩余项接收，完整独验及工厂排程租约成功才 accepted。
- `readback(principal, publication_id, *, action_id)`：独立读取 receiver 持久化最新签名回执，不以发布对象缓存作证据；unknown 只有查到权威回执且验证后才能解除。无回执不能当未发送。
- `cancel_publication(principal, publication_id, *, action_id, note)`：接收器权威撤销；交付前先写取消 tombstone，后续迟到递送不能复活；partial/accepted但未执行可取消；执行开始后明确 too_late、不回滚。
- `execute_synthetic(principal, publication_id, *, action_id, confirmed)`：另行确认，只有 authoritative accepted、source当前有效、完整工厂租约存在才执行原创事件模拟；running→completed/failed。结果与事件持久化，不以 accepted 冒充 completed。

所有人类动作按 owner+plant+active/ACL 重新鉴权；review有角色要求，其余owner合法用户可做。receipt 幂等绑定全部请求/hash/body；回放前重验身份和目标。相同action ID不同请求拒绝。

## 消息真实性与密钥

`receiver.py` 提供 `LocalReceiver(store)`，keys 在 Store 私有根目录独立秘密文件，32bytes以上系统随机值，目录0700/文件0600，无软链接/硬链接/宽权限/大小异常；独占初始化，不把密钥存公共objects/artifacts或API。已有接收器数据库却密钥丢失时 failclosed，不能重建密钥伪造连续性。main会在内容/忽略审查确认不上传。

HMAC-SHA256，域分隔请求/回执/取消；canonical有限JSON、常数时间比较、strict schema。payload 包含协议version、owner/plant、publication ID、recommendation hash、plan hash、source binding、全部所需operation IDs、created/expiry、synthetic=True。签名绑定原始完整字节语义，不能相信caller提供的hash；model无签名权限。

接收器 entry 方法由 backend 持有的 capability（实例identity验证）调用；不提供HTTP签名/接收callback接口。测试可直接调用可信适配器检查伪签名/错误capability/乱序回执，不给浏览器控制权威状态。共享HMAC不提供秘密持有方之间的不可否认性。

## 状态与部分接收

publication payload 独立 `delivery_status`=not_sent/unknown/acknowledged 和 `receiver_status`=none/partial/accepted/cancelled、`execution_status`=none/running/completed/failed；object.status 给UI简明requested/unknown/partial/accepted/cancelled/running/completed/failed，不覆盖内部多维事实。

首次递送接收器固定按operation ID排序，若>1项仅stage第一项，返回partial；单项可全接收。partial不占料、不执行。advance接收全部剩余项，重新核验来源、冻结候选与独验，再原子取得完整工厂排程租约。

**保守整厂语义**：每候选是该合成工厂当前完整排程，不是可并行叠加的局部工单。接收器每plant同一时刻最多一个active完整计划，跨owner也不例外；冲突只报busy不泄漏他人对象。该租约比逐区间资源预留更保守，防止两个已独验计划重复使用同份库存/资源，不声称优化跨计划并行。unknown递送保留发布意图，不允许换新publication绕过。

取消未执行计划释放工厂租约一次。执行开始租约不可取消；执行完成/失败保留已使用 source epoch 与 operation IDs（不能同快照再次运行或换ID重发重复执行），需导入更新 revision 且新的观察时间、并明确新的未执行operation IDs 才允许后续计划。导入新快照不抹除旧执行事实。partial无租约，执行必须完整接收且一致；错误接收不能留下半份租约。

## 崩溃、乱序与取消

1. 调用前持久化delivery unknown+action保留，再离开事务调用接收器；receiver写状态+签名receipt同事务，之后ACK更新publisher另事务。
2. ACK丢失不重发：readback读权威receiver记录恢复；publisher标记不是权威事实。
3. receiver回执递增sequence且累计IDs。reducer验证签名/来源、序列对应权威最新记录；同seq同内容no-op，不同内容拒绝；旧seq不回退，未知新seq不可凭空接受。
4. cancel/complete receive/execute admission 用BEGIN IMMEDIATE串行化；迟到ACK不能覆盖cancelled/running/completed/failed。已执行历史不可撤销，未知动作不凭空结清。
5. execute admission先在同一事务持久化源epoch、已使用operation IDs及running，再离开事务运行simulation，结束后保存真实结果。崩溃遗留running不得自动重跑；独立readback若无最终执行事件应保留unknown/running并要求人工核查，不伪造完成。

## 验收

真实本地签名/回执/数据库与事件引擎，不stub。测试包括篡改签名/内容/来源/owner、密钥重启/丢失/链接、部分接收不得执行、跨owner整厂计划冲突、失ACK独立回读、重复和乱序回执、取消与ACK/执行竞争、同快照重复运行拒绝、source stale、仿真失败保留事件。所有结果标识synthetic；不表示设备实际加工。
