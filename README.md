# VRChat玩家查询及审核

AstrBot 插件：查询 VRChat 玩家 / 地图信息，并支持「VRC 玩家 QQ 群」的入群审核。

- 玩家信息：昵称、玩家 ID、正在使用的模型、正在展示的群组、当前位置 + 房间 ID
- 地图信息：图标、名字、地图 ID、地图上传者
- 入群审核：新成员回答问题后自动查询其 VRChat 信息，转发到指定管理群，管理群成员引用回复「同意 / 拒绝」完成进群审核
- 登录会话持久化：登录成功后复用 auth cookie，不掉线、不重复登录

> 数据来源：非官方 VRChat API（[vrchat.community](https://vrchat.community/)）。滥用可能导致账号受限，请合理使用。

---

## 目录结构

```
astrbot_plugin_vrc_tool/
├── main.py            # 插件主文件（指令 + 入群审核）
├── vrc_api.py         # VRChat API 客户端（登录/2FA/会话持久化/查询）
├── metadata.yaml      # 插件元数据
├── _conf_schema.json  # WebUI 配置项定义
├── requirements.txt   # 依赖（aiohttp）
└── README.md          # 本文档
```

---

## 部署操作

插件适配 `aiocqhttp`（OneBot v11 / NapCat / go-cqhttp / Lagrange 等 QQ 协议端）。AstrBot 需为 v4.16+。

### 方式一：WebUI 安装（推荐）

1. 将本插件目录打成 zip（zip 内直接包含 `metadata.yaml`、`main.py` 等文件，不要多包一层文件夹）
2. 打开 AstrBot WebUI → 插件 → 右上角「安装插件」→ 选择 zip 上传
3. 更新插件时：重新上传新的 zip 覆盖安装，然后在插件管理中点「重载插件」

### 方式二：手动复制

1. 将 `astrbot_plugin_vrc_tool` 整个目录复制到 AstrBot 的 `data/plugins/` 下
2. 在 WebUI 插件管理中启用并「重载插件」

### 部署前必读

- 机器人账号必须是**玩家群的管理员或群主**（入群审核的前提，否则收不到加群请求事件）
- 登录用的 VRChat 账号需要与待查询玩家**互为好友**，或对方公开位置，才能查到「当前位置/房间 ID」
- 插件会调用 `api.vrchat.cloud`，请确保运行 AstrBot 的服务器可正常访问外网

---

## 使用说明

指令可通过 `/指令` 或 `@机器人 指令` 触发。所有指令前均以 `/` 示例。

### 1. 登录 VRChat

```text
/vrc登录 <邮箱> <密码>
```

- 登录成功后会提示会话已保持
- 若账号开启了两步验证（邮箱验证码），会提示验证码已发送到绑定邮箱：

```text
/vrc验证 <验证码>
```

- 验证码有效期很短，每次执行 `/vrc登录` 都会发送一封新邮件并使旧验证码失效，请使用邮箱中**最新一封**
- 查看当前登录状态：`/vrc状态`

> 会话（auth cookie）会持久化到 `data/plugin_data/astrbot_plugin_vrc_tool/auth.json`，重启插件后自动恢复，不会重复登录掉线。

### 2. 查询玩家

```text
/vrc玩家 <玩家昵称或玩家ID>
```

自动判断：以 `usr_` 开头按玩家 ID 查询，否则按昵称搜索（精确匹配优先）。

在线玩家示例输出：

```
━━━ VRChat 玩家 ━━━
▸ 昵称　　：TestUser
▸ 玩家ID　：usr_xxx
▸ 正在使用的模型：模型名
▸ 正在展示的群组：群组名（grp_xxx）
▸ 当前位置：地图名（wrld_xxx）
▸ 房间ID　：123~hidden...
```

不在线或位置未公开时，省略「当前位置 / 房间 ID」。

### 3. 查询地图

```text
/vrc地图 <地图昵称或地图ID>
```

自动判断：以 `wrld_` 开头按地图 ID 查询，否则按昵称搜索。输出地图缩略图 + 名字 + 地图 ID + 上传者。

### 4. 入群审核

适用场景：QQ 群设置「需要回答问题并由管理员审核」。机器人收到加群申请后，把申请人回答的内容当作 VRChat 玩家昵称 / ID 去查询，然后将玩家信息发送到管理群供审核。

流程：

1. 新成员申请入群并回答问题
2. 机器人自动查询该玩家信息，并转发到该玩家群对应的**所有管理群**，消息含：
   - 玩家群号（群名）、申请人 QQ（昵称）、入群问题与答案
   - VRChat 玩家信息
3. 管理群内任意成员**引用该审核消息**回复「同意」或「拒绝」
4. 机器人调用 `set_group_add_request` 完成进群 / 拒绝操作（任一管理群先处理即生效，其他管理群再回复会提示已处理）

管理群内收到的审核消息示例：

```
━━━ 入群审核申请 ━━━
▸ 玩家群　：123456789（VRChat玩家群）
▸ 申请人　：987654321（申请人昵称）
▸ 入群问题：请回答你的 VRChat 玩家昵称
▸ 申请答案：TestUser
──── VRChat 信息 ────
━━━ VRChat 玩家 ━━━
▸ 昵称　　：TestUser
▸ 玩家ID　：usr_xxx
▸ 正在使用的模型：模型名
▸ 正在展示的群组：群组名（grp_xxx）
▸ 当前位置：地图名（wrld_xxx）
▸ 房间ID　：123~hidden...
────────────────────
请引用本消息回复「同意」或「拒绝」进行审核。
```

审核有效期默认 1 小时（`review_expire_seconds`），超时后引用回复无效。

### 5. 配置项（WebUI 插件管理）

| 配置项 | 类型 | 说明 |
|---|---|---|
| `vrc_username` | string | VRChat 登录邮箱 / 用户名（可选，配置后可自动登录；也可用 `/vrc登录`） |
| `vrc_password` | string | VRChat 登录密码（可选，明文保存在配置中，谨慎使用） |
| `app_name` | string | 调用 VRChat API 的应用标识（User-Agent），默认 `astrbot-plugin-vrc-tool` |
| `request_interval` | int | VRChat API 请求最小间隔（秒），建议 ≥1，避免触发风控 |
| `join_review_enabled` | bool | 是否启用入群审核 |
| `review_groups` | list | 玩家群 → 管理群映射，条目格式：`玩家群号:管理群1,管理群2`（一个玩家群可对应多个管理群，可添加多条） |
| `join_question` | string | 玩家群设置的入群问题原文（展示给管理群参考，也有助于从 comment 中剥离问题） |
| `review_expire_seconds` | int | 审核消息有效时间（秒），默认 3600 |

示例配置：

```
review_groups:
  - "100001:200001,200002"   # 玩家群100001 的审核消息发到 200001、200002 两个管理群
  - "100002:200003"
```

> VRChat User-Agent 的联系信息已在代码中固定为 `support@baidu.com`（见 `main.py` 中 `VRC_API_CONTACT`），如需更改请直接修改该常量。

---

## 常见问题排查

| 现象 | 排查方式 |
|---|---|
| 登录提示「需要邮箱验证码」但验证总失败 | 用邮箱**最新一封**邮件里的验证码；若仍失败，看控制台 `[VRC工具]` 日志（含 HTTP 状态与返回体） |
| 登录返回 403 user-agent | 联系信息被 VRChat WAF 拒绝，将 `VRC_API_CONTACT` 改为你的真实可联系邮箱 |
| 加群申请后管理群收不到审核 | 看控制台 `[入群审核]` 日志：无「收到群加群请求」说明机器人不是玩家群管理；有日志但没发送说明 `join_review_enabled` 未开或 `review_groups` 群号不一致 |
| 查询在线玩家不显示位置/房间 ID | VRChat 隐私策略：需查询账号与目标互为好友，或目标公开位置 |
| 引用回复「同意/拒绝」没反应 | 确认回复的是机器人发出的审核消息、消息文本以「同意/拒绝」开头，且在 `review_expire_seconds` 内 |

日志前缀：`[VRC工具]`（登录/查询）、`[入群审核]`（入群审核）。

---

## 安全与合规提示

- VRChat API 为社区维护的非官方接口，请遵守 [VRChat Creator Guidelines](https://hello.vrchat.com/creator-guidelines#api-usage)，控制请求频率
- `auth.json` 中保存了 VRChat 登录凭据与会话 cookie，请勿外泄；不要在不受信任的环境运行
- 入群审核的「同意 / 拒绝」对管理群内所有成员开放，请确保管理群成员可信

---

## 贡献者

欢迎参与维护与贡献。目前主要维护信息：

| 角色 | 名称 |
|---|---|
| 作者 / 维护者 | Mcsakura_樱佬 |
| 作者 / 维护者 | DeepSeek-V4-Flash |

贡献方式：提交 issue / Pull Request，说明改动用途与测试结果即可。

项目灵感与参考：
- VRChat API 非官方文档 [vrchat.community](https://vrchat.community/)
- AstrBot 插件开发指南 https://docs.astrbot.app/dev/star/plugin-new.html
