# 餐饮匿名接口

餐饮查询优先由模型使用真实 HTTP POST 工具按本文件直接请求。只有直接获取失败、缺少可用 POST 工具或关键数据无法可靠取得时，才用附带的原有 Python 脚本补抓；有效空数据不等于请求失败。补抓沿用条件并保留首次请求的来源和失败记录。

全部为 JSON POST。主动设置 `Content-Type: application/json` 和 `Accept: application/json`。不用 Cookie、Authorization、用户 UUID 或其他会话字段，不跟随重定向，不关闭 TLS 校验。工具若附带无法禁用的用户会话，不能声称匿名。

临时网络错误、408 / 429 / 5xx 或缺失关键响应数据时，自动在同一只读请求上最多尝试 3 次，可退避 1 秒、2 秒；有效空数组不重试，登录跳转和 401 / 403 不换登录态重试。依赖分组 / 店铺 ID 的请求先完成，再以 4 至 8 路并发查询独立店铺菜单；成功响应保留，不随其他店铺失败而整批重抓。包内餐饮查询和比价保底脚本均内置请求重试，比价还内置并发。

大范围查询在首次读取时保留全部商品、分类、规格、价格、状态及每店成功／空菜单／失败统计，再由模型判断符合项并排序。每店成功菜单仅取一次；改筛选策略或修复本地统计不重复请求。工具没有原生并发或无法完整保留数据时，在遍历前按执行障碍使用包内 `catalog.py`；不临时写串行或 runspace 采集器。首次说明和比价参考已有正常字段口径，不为核对口径预读脚本源码。

## 食堂分组

```text
POST https://m.kplocker.com/mm/wx/api/shop/categories
```

```json
{
  "bizZoneId": "185",
  "couponUseChannel": "synjonesApp",
  "cateringChannel": "SynjonesApp"
}
```

`data` 为分组数组，每组包含 `bizZoneId`、`bizZoneName`、`name` 和逗号分隔的 `shops`。仅接收学校编号为 `185` 的组，按本次实际名称匹配食堂，再解析有效数字 ID；不从示例或历史抓包拷贝 ID。

2026 年 11 月 13 日已在无凭证的新客户端中验证该接口可返回学校分组。该日期是接口验证时间，不是后续用户查询的数据时间。

## 商户

```text
POST https://m.kplocker.com/mm/wx/api/shop/gets
```

```json
{
  "statuses": ["Open", "Pause"],
  "startRow": 0,
  "limitRows": 9,
  "bizZoneId": 185,
  "sortField": "sort desc",
  "diningTypes": [],
  "couponUseChannel": "synjonesApp",
  "cateringChannel": "SynjonesApp"
}
```

`data` 为商户数组。按 9 递增 `startRow`，不足 9 条或全部实时分组店铺均已取得时停止。普通选餐默认最多 6 页，明确需要时最多 12 页；比价默认持续遍历全部店铺，不沿用这两个页数上限。遇到失败先自动重试，仍失败或反复出现无新增店铺的循环页面时记录覆盖缺口。去重并保留 ID、名称、`openStatus` 和查询时间。数据有变化时分页可能重复或遗漏，不能宣称快照事务一致性。

默认增加 `shops` 字段，值为本次学校分组返回的所选档口 ID，用逗号连接；不添加猜测或历史 ID。超出本次 ID 集合的返回记录应排除。分组查询失败才回退到不带 `shops` 的学校列表，标明范围未按分组核实。

`status` 为基础状态，当前可点、预订或暂停看 `openStatus`。不将同名但不同 ID 的商户擅自合并。

## 菜单

```text
POST https://m.kplocker.com/mm/wx/api/shop/all/gets
```

```json
{
  "status": "Open",
  "id": "本次商户列表实际返回的ID",
  "couponUseChannel": "synjonesApp",
  "cateringChannel": "SynjonesApp"
}
```

替换真实 ID 后发送，不猜测 ID。`data.shop.bizZoneId` 应为数字 `185`，并展示或核对实际校名；其他学校排除，字段缺失则不能确认校内范围。

`data.items` 为商品数组，`categories` 可辅助判断商品类型。保留列表状态，详情不一定重复包含 `openStatus` 或 ID。

金额单位为分。实际菜价按 `discountPrice + 所选 itemAttrs[].specs[].money` 计算，不使用未确认的团购价。选项组中的 `default` 可作为真实默认选项；无默认时应明确提出所选项。包装费与配送等费用分开说明。

## 排队

```text
POST https://m.kplocker.com/mm/wx/api/shop/order/accepted/blocked
```

```json
{
  "id": "本次商户列表实际返回的ID",
  "couponUseChannel": "synjonesApp",
  "cateringChannel": "SynjonesApp"
}
```

最多查询两个相关档口。`data.blockedOrders` 为平台排队订单数，`data.waitTime` 为平台预计等待分钟；都应是实际数值，缺少数据则未知。匿名响应曾出现业务码成功但无 `data`，不能断定原因，不能改用抓包凭证。

## HTTP 工具回退的证据

记录实际可见 HTTP 状态、业务码、`data` 是否存在、查询时间和查到的数量。工具没有暴露的部分标明未提供；网页读取成功不代表执行了 POST。其他价格、状态和推荐边界仍按 [餐饮模块](dining.md)。
