# 中央空调教学软件算法支持服务

本项目是面向中央空调课程教学的软件后端服务，使用 Python 实现算法、FastAPI 提供接口，
并通过 Vue 工作台提供课堂操作界面，
为学生提供空调系统搭建后的负荷计算、风水系统计算、拓扑校验、设备参数查询、稳态仿真、
实时仿真、能耗估算和教学报告能力。

系统重点强调算法过程的可观察性、计算结果的可解释性以及错误和整改建议的教学反馈。
它采用工程估算和教学级动态模型，用于课堂演示、实验和方案理解，不替代正式施工图设计、
厂家选型或第三方审图计算。

## 快速开始

```powershell
cd "E:\xiangmu\JiZhongKongTiao\中央空调搭建（可编辑）\suanfaptaizhichi"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

| 入口 | 地址 |
| --- | --- |
| 健康检查 | `GET http://127.0.0.1:8010/health` |
| 自动接口文档 | `http://127.0.0.1:8010/docs` |
| 前端工作台 | `http://127.0.0.1:8010/` |

工作台可切换或保存教学样例，在“建筑结构”页用表单编辑楼层、房间、设备、管路和连接，
并运行系统评估、实时仿真，查看历史、参数库与服务诊断。

## 目录结构

```
suanfaptaizhichi/
├── app/
│   ├── main.py                 # FastAPI 应用与全部路由
│   ├── schemas.py              # Pydantic 请求/响应模型（核心契约）
│   ├── building.py             # 建筑/楼层/房间节点归属模型
│   ├── project_validation.py   # 项目格式校验
│   ├── realtime.py             # 实时 session、控制与订阅
│   ├── equipment_selection.py  # 教学设备选型
│   ├── equipment_knowledge.py  # 设备参数知识（只读）
│   ├── teaching_scenarios.py   # 教学方案对比
│   ├── reporting.py            # 报告生成
│   ├── samples.py              # 内置示例系统
│   ├── simcore/                # 统一仿真核心（实时 + 稳态）
│   ├── calculators/            # 负荷/风/水/管网/能耗/拓扑算法
│   ├── hardy_cross/            # Hardy-Cross 管网求解器
│   ├── steady_solver/          # 稳态端口图求解器
│   ├── validators/             # 校验
│   ├── data/                   # 参数库、规则库、项目存储
│   ├── static/                 # 工作台构建产物
│   └── unity_catalog/          # 设备角色与几何词汇表（DeviceRole）
├── web/                        # Vue 3 工作台源码
├── docs/                       # 设计与契约文档
├── tests/                      # pytest 测试
└── sample_system.json, requirements*.txt
```

## 接口总览

完整字段、控制范围、WS 事件和错误语义见 [`docs/接口文档.md`](docs/接口文档.md)。

**项目管理**

- `GET|POST /api/v1/projects`
- `GET|PUT|DELETE /api/v1/projects/{project_id}`
- `POST /api/v1/projects/{project_id}/evaluate`
- `GET /api/v1/projects/{project_id}/history`
- `POST /api/v1/project/validate`
- `GET /api/v1/project/sample/{sample_name}`

**计算与评估**

- `POST /api/v1/load/calculate`
- `POST /api/v1/air/calculate`
- `POST /api/v1/water/calculate`
- `POST /api/v1/topology/analyze`
- `POST /api/v1/hydraulic-network/calculate`
- `POST /api/v1/air-network/calculate`
- `POST /api/v1/simulation/steady-state`
- `POST /api/v1/energy/simulate`
- `POST /api/v1/system/evaluate`
- `POST /api/v1/report/generate`、`POST /api/v1/report/download`

**实时仿真**

- `POST /api/v1/realtime/simulate/start/{project_id}`、`.../stop/{project_id}`
- `GET|POST /api/v1/realtime/simulate/config/{project_id}`
- `GET /api/v1/realtime/state/{project_id}`、`POST /api/v1/realtime/update`
- `GET /api/v1/realtime/rooms/{project_id}`
- `GET /api/v1/realtime/pipes/{project_id}`
- `GET /api/v1/realtime/history/{project_id}`
- `POST /api/v1/realtime/control/{project_id}`
- `GET /api/v1/realtime/controls/{project_id}`
- `POST /api/v1/realtime/controls/reset/{project_id}`
- `GET /api/v1/projects/{project_id}/devices/rated`
- `WS /ws/realtime/{project_id}`

**参数库与知识**

- `GET|PUT /api/v1/catalog/equipment`
- `POST /api/v1/catalog/equipment/item`、`DELETE .../item/{device_type}/{name}`
- `GET /api/v1/catalog/equipment/quality`、`GET .../template`、`POST .../import`
- `GET /api/v1/rules`
- `GET /api/v1/knowledge/equipment`、`GET .../{knowledge_id}`
- `POST /api/v1/selection/recommend/{project_id}`
- `GET /api/v1/teaching/scenarios`、`POST /api/v1/teaching/compare/{project_id}`

**服务状态**

- `GET /api/v1/diagnostics`

## 算法能力

**静态计算与校验**

- 冷热负荷、风量、水量、管径/风管尺寸、阻力和设备校验。
- 设备参数表：按设备类型和型号从 `app/data/equipment_catalog.json` 匹配容量、流量、扬程、功率、支持系统。
- 拓扑校验：孤立节点、未知节点、设备/管路系统混接、冷冻水路径、冷却水路径、水环路开闭。
- 多支路水力：按冷冻水、冷却水、热水系统分配流量，计算每条管段流速、阻力、扬程和最不利管段。
- 风系统管网：风管风量、风速、压降和风机压力参考值。
- 规则库：`app/data/rules.json` 管理风速、水速、泵余量、阻力默认参数。
- 报告输出：汇总负荷、设备、水力、风网、问题和整改建议。

**统一仿真核心**

实时与稳态都由 `app/simcore` 计算，稳态是动态核在设计工况下的不动点。

- 设备参数驱动：冷机、热泵、泵、冷却塔和末端从型号目录读取额定值，降级来源通过 API 显式返回。
- 物理联动：水泵相似定律、冷热负荷/流量反算温差、冷却塔冷凝反馈、部分负荷 COP 和房间热容积分。
- 水系统闭环：冷冻水/热水末端按额定流量、阀位和直连供回水管的相对阻力分配水量；供水温逐 Tick
  趋近冷热源有效目标，支路回水按最终负荷计算后流量加权混合，水泵、末端与冷热源共用同一守恒结果。
- 单一状态源：Vue 工作台通过 REST/WS 读写服务端唯一状态，稳态与实时不会算出不一致的结果。

## 实时仿真行为

### 仿真参数与房间状态

`GET|POST /api/v1/realtime/simulate/config/{project_id}` 设置全项目的 `initial_room_temp_c`、
`target_temp_c` 和 `target_humidity_pct`。温度范围为 `10.0～40.0 C`，目标湿度范围为
`30.0～80.0%`；`initial_room_temp_c=null` 时按兼容规则使用 `outdoor_base_temp_c - 3.0` 作为
新 session 初始室温。额外字段、`NaN` 和正负无穷返回 HTTP 422。运行中修改模式或房间初始状态返回
409；目标、室外条件和故障等允许字段在下一个 tick 生效，停止状态下改变受保护字段才会重建 session。
重复提交相同配置不会重置已推进的房间温度。

`GET /api/v1/realtime/rooms/{project_id}` 按项目中的房间顺序返回房间/楼层标识、当前温度、目标温度和
`relative_humidity_pct`，温度与湿度保留两位小数。湿度由 `RoomInput.indoor_relative_humidity` 在 tick 0
播种，之后由 simcore 按湿度比动态积分并限制在 `10.0～95.0%`；它不是每次读取都从保存的项目常量重算。
仿真未启动时返回不缓存的 tick-zero 初始快照，该读取不启动后台仿真、不追加历史、不广播 WebSocket、不写入
项目。未知项目返回 404，无房间项目返回 HTTP 200 和空列表。

### 自动调节与模式联锁

realtime 默认启用教学化自动调节，根据模式与室温偏差有界地改变设备启停、运行台数、泵/风机转速和阀门
开度；稳态求解仍保持手动控制语义。制热模式使用热源与热水泵，制冷模式使用冷机、冷冻/冷却水泵与冷却塔，
模式联锁不能被手动开启绕过。实时汇总分别返回 `chilled_water_flow_m3_h`、`cooling_water_flow_m3_h`、
`hot_water_flow_m3_h`、`heating_load_kw` 及热水供回水温。

### 水温动态

对项目已声明的冷冻水或热水环路，未接水的末端只保留风侧运行值，不产生水侧流量和冷热量；完全没有水拓扑
的旧项目保持额定流量兼容回退。降流量时盘管能力按流量比平方根有界衰减，使末端能力下降的同时供回水温差
增大。新 session 的冷冻水和热水均从初始平均室温开始，供水温按稳定的一阶教学响应逐 Tick 接近设定值及
容量不足修正目标；回水温由有效末端支路的最终流量和冷热量混合。停源、停泵或无有效支路时冷热量为零且
回水温等于供水温。

该模型不包含管段传播延迟、管道散热、水锤或工程级泵工作点。支路阻力只用于教学化相对分流，未求解泵工作点
或工程压差，不替代管网水力计算。

### 实时管段读数

`GET /api/v1/realtime/pipes/{project_id}` 返回每根水管当前 tick 的流量、流速、压降和服务的末端。流量由
末端实时水量沿项目连接方向累加：供水管取下游末端之和，回水管取上游末端之和，冷却水按单环路取全部环路
流量；流速与压降复用静态水力的 `calculate_water_from_flow`。响应另给各系统总流量（取主管流量，逐管相加
会把同一股水在供回水管上数两遍）和流速最高的管段。

这是按流量反算，不是压力平衡求解——管段流量完全信任 simcore 的支路分流结果，因此看不出"管径过小形成
压力壁垒把流量挤到别处"，那种情况只表现为该管流速偏高。它不替代 `hydraulic-network` 的设计校核，也不是
工程平差。与 `realtime/rooms` 同为只读投影：仿真未启动时返回 tick-zero 快照，不创建 session、不追加
历史、不广播 WebSocket、不写入项目。

### 手动控制

手动控制按设备标记 `control_source="manual_override"`，其他设备继续自动调节。
`POST /api/v1/realtime/controls/reset/{project_id}` 用 `{"device_instance_id":"pump_1"}` 清除单台覆盖，
用 `{}` 或字段为 `null` 清除全部覆盖；已启动的仿真停止后也会清除覆盖。故障注入只覆盖当前 tick，不写入
手动控制基础值。额定容量、流量、COP 和型号仍只由 rated API 返回，不会伪装成实时变化值。

## 设备参数知识

`GET /api/v1/knowledge/equipment` 按固定顺序返回 11 类设备的知识摘要；
`GET /api/v1/knowledge/equipment/{knowledge_id}` 按区分大小写的稳定 ID 返回完整用途、工作原理、主要
部件、关键参数含义和适用系统。未知 ID 返回 HTTP 404，`detail="Equipment knowledge not found."`。

`冷源机组`、`循环泵`、`冷却塔`、三类末端以及`空气源热泵`、`水源热泵`、`地源热泵机组`的
`catalog_linked=true`，`catalog_device_type` 指向现有设备目录类型，参数中的 `catalog_field` 只引用目录
真实字段，不复制型号额定值。`换热器`和`压缩机`仍为纯知识条目。

三类热泵各提供 3 个可选教学型号和 1 个 `name="*"` 类型回退行，统一使用 `role="heat_source"`、
`systems=["hot_water"]`。`capacity_kw` 表示额定制热量，`flow_m3_h` 表示负荷侧热水流量，`power_kw`
表示输入功率，`cop` 表示额定制热 COP。空气侧、源水侧和地埋管侧差异由知识条目解释，不作为新的可连接
系统建模。这些通用示例值不构成厂商性能承诺、采购或施工设计依据。

知识内容随后端版本只读发布，不依赖项目、可编辑设备目录或 realtime session；相同版本的
重复请求返回相同内容和顺序。

## 教学设备选型

```http
POST /api/v1/selection/recommend/{project_id}
Content-Type: application/json

{"target_role":"pump"}
```

`target_role` 仅允许 `cold_source`、`heat_source`、`pump`、`terminal`，额外字段返回 422。调用方不能
提交负荷、流量、扬程、风量、台数或裕量；服务使用现有负荷、水力和风量计算器从项目派生这些条件，并用
`equipment_catalog.json` 和 `rules.json` 评价所有实际型号。目录中的 `name="*"` 默认回退项不参与候选。

响应中负荷/功率为 `kW`，流量和风量为 `m3/h`，扬程为 `m`，数值统一保留 3 位小数。台数聚合规则：

| 角色 | 台数依据 | 聚合方式 |
| --- | --- | --- |
| 冷源 | 容量裕量 | 容量、冷冻水流量、功率相加 |
| 热源 | 供热容量裕量与热水流量取较大台数 | 功率相加，COP 保持单机额定展示值 |
| 水泵 | 流量需求 | 流量、功率相加，扬程保持单泵额定值 |
| 末端 | 容量、水流量、送风量三项下限取最大台数 | 按台数相加 |

成功响应均为 HTTP 200：`recommended` 表示至少一个候选合格，`no_eligible_candidates` 表示有型号但全部
不合格，`no_catalog_candidates` 表示该角色没有实际型号。未知项目返回 404；项目缺少房间、对应角色所需的
正数负荷/流量，或水泵所需正扬程时返回 `detail.code="SELECTION_REQUIREMENTS_UNAVAILABLE"` 的结构化 422。

选型查询只读：不修改目录、项目文件或历史，也不创建、推进或改变实时 session、控制值和快照。结果用于教学
解释，不替代施工图设计、厂家复核或采购决策。

## 单位约定

| 量 | 单位 |
| --- | --- |
| 面积 | `m2` |
| 高度 / 扬程 | `m` |
| 温度 | `C` |
| 负荷 / 功率 | `kW` |
| 风量 / 水量 | `m3/h` |
| 管径 / 风管尺寸 | `mm` |
| 阻力 | `Pa` |
| 转速 / 阀门开度 | `%`（0-100） |

## 示例

内置样例通过 `GET /api/v1/project/sample/{name}` 读取：`complete`、`air`、`bad`、`multi_floor`、
`four_floor_fcu_fresh_air`。工作台提供 `complete` 和 `four_floor_fcu_fresh_air` 两个未保存样例；后者
包含负一层机房、地上三层、风机盘管加新风，以及冷冻水、冷却水、热水和空气系统，冷水机组承担制冷，
屋面空气源热泵承担制热。

四层样例用于教学校核。实际工程通常还需要膨胀补水、水处理、过滤器、平衡与控制阀、冷凝水排放、防火排烟、
计量和自动控制，不能直接作为施工或采购依据。

```powershell
$body = Get-Content .\sample_system.json -Raw
Invoke-RestMethod `
  -Uri http://127.0.0.1:8010/api/v1/system/evaluate `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

## 开发

后端测试与静态检查（`ruff`、`mypy` 见 `requirements-build.txt`）：

```powershell
pytest
ruff check app tests
mypy app
```

前端（`web/`，Vue 3 + Vite + Pinia）：

```powershell
cd web
npm install
npm run dev          # 开发服务器，代理 /api、/health、/ws 到 127.0.0.1:8000
npm run test:unit
npm run typecheck
npm run build        # 产物写入 ../app/static，由 FastAPI 挂载在 / 和 /static
```

`npm run dev` 的代理指向 8000 端口，与后端默认示例的 8010 不同；开发时用
`--port 8000` 启动 uvicorn，或改 `web/vite.config.ts` 中的代理目标。

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/接口文档.md`](docs/接口文档.md) | 接口字段、控制范围、WS 事件、错误语义 |
| [`docs/项目文档.md`](docs/项目文档.md) | 模块结构、算法公式推导、Issue 码汇总 |
| [`docs/算法深读.md`](docs/算法深读.md) | 算法原理深入说明 |
| [`docs/hardy_cross_design.md`](docs/hardy_cross_design.md) | Hardy-Cross 求解器设计 |
