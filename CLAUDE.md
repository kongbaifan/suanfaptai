# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

中央空调教学软件算法支持服务 —— Python + FastAPI 后端，Vue 3 前端工作台。

面向中央空调课程教学，提供负荷计算、风水系统计算、拓扑校验、设备参数查询、稳态仿真、实时仿真、能耗估算和教学报告能力。强调算法过程可观察性、计算结果可解释性和教学反馈。

**定位**：工程估算和教学级动态模型，用于课堂演示、实验和方案理解，不替代正式施工图设计、厂家选型或第三方审图计算。

## 核心命令

### 后端开发

```powershell
# 环境搭建
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 启动开发服务器（默认 8010 端口）
uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload

# 测试
pytest                              # 运行全部测试
pytest tests/test_api.py            # 运行单个测试文件
pytest -k test_load_calculate       # 运行匹配名称的测试

# 代码质量检查（需要 requirements-build.txt）
ruff check app tests
mypy app
```

### 前端开发

```powershell
cd web
npm install

# 开发服务器（代理 /api、/health、/ws 到 127.0.0.1:8010）
npm run dev

# 测试与类型检查
npm run test:unit
npm run typecheck

# 构建（产物写入 ../app/static，由 FastAPI 挂载）
npm run build
```

**前端 5173、后端 8010**：`npm run dev` 的代理目标与后端默认端口一致，按上面的
文档启动 uvicorn 即可直接联调，无需额外改端口。

## 架构关键

### 数据契约：schemas.py

`app/schemas.py` 是核心契约层，定义全部 Pydantic 请求/响应模型。修改字段时必须同步更新接口文档和前端类型。

### 统一仿真核心：simcore/

`app/simcore/` 是稳态与实时仿真的统一计算核心。稳态是动态核在设计工况下的不动点。

- **单一状态源**：每个 `project_id` 持有唯一 session，REST 与 WebSocket 共用同一份状态，Vue 工作台不计算任何暖通数值
- **物理联动**：水泵相似定律、冷热负荷/流量反算温差、冷却塔冷凝反馈、部分负荷 COP、房间热容积分
- **水系统闭环**：冷冻水/热水末端按额定流量、阀位和相对阻力分配水量；供水温逐 tick 趋近冷热源目标；回水温按流量加权混合
- **固定次数迭代**：水侧与冷凝侧互为输入，采用固定迭代次数（非残差判据）保证每 tick 耗时可预测，避免实时推送偶发卡顿

核心文件：
- `session.py` (2500+ 行)：仿真会话主循环，tick 推进逻辑
- `devices.py`：冷机、热泵、泵、冷却塔、末端设备状态计算
- `hydronics.py`：水力分支分配、温度推进、回水混合
- `rated.py`：从设备目录读取额定参数，降级来源显式返回
- `psychrometrics.py`：湿度比、焓值、露点计算（Magnus-Tetens 饱和水蒸气压）

### 算法模块：calculators/

静态计算器，被 `/api/v1/system/evaluate` 和稳态求解器调用：

- `load.py`：冷热负荷计算（分项法：人员、照明、设备、新风显热/潜热、太阳辐射、围护得热）
- `air.py` / `air_network.py` / `air_tree.py`：风量、风管尺寸、风系统管网（风速、压降、风机压力）
- `water.py` / `hydraulic_network.py`：水量、管径、阻力、扬程、多支路水力（按冷冻水/冷却水/热水系统分配流量，最不利管段）
- `topology.py`：拓扑校验（孤立节点、未知节点、系统混接、冷冻水/冷却水路径、水环路开闭）
- `network_paths.py`：水力管网路径提取（环路识别）
- `energy.py`：能耗估算
- `simulation.py`：稳态求解器适配层

### 数据与规则库：data/

- `equipment_catalog.json`：设备参数表（按设备类型和型号匹配容量、流量、扬程、功率、支持系统）
- `rules.json`：规则库（风速、水速、泵余量、阻力默认参数）
- `projects/*.json`：项目存储
- `migrations/`：数据迁移脚本

### 实时仿真行为

- **自动调节**：根据模式与室温偏差有界地改变设备启停、运行台数、泵/风机转速和阀门开度
- **模式联锁**：制热模式使用热源与热水泵，制冷模式使用冷机、冷冻/冷却水泵与冷却塔，不能被手动开启绕过
- **水温动态**：供水温按一阶教学响应逐 tick 接近设定值；回水温由有效末端支路流量和冷热量混合；末端盘管按 ε-NTU 逆流换热器计算，降流量时 UA 按 `V̇^0.8` 衰减且 `C_max` 同步下降，双重削弱末端能力
- **手动控制**：按设备标记 `control_source="manual_override"`，其他设备继续自动调节；停止后自动清除覆盖
- **配置保护**：运行中修改模式或房间初始状态返回 409；目标、室外条件和故障允许在下一 tick 生效

### Hardy-Cross 管网求解器：hardy_cross/

水力平差求解器，用于复杂管网流量分配。详见 `docs/hardy_cross_design.md`。

### 设备角色词汇表：unity_catalog/

`DeviceRole` 枚举定义设备角色（`cold_source`、`heat_source`、`pump`、`terminal` 等），用于设备选型、拓扑校验和 Unity 桥接。

## 开发约定

### 修改算法时

1. **先读文档**：`docs/项目文档.md`（模块结构、算法公式推导）、`docs/算法深读.md`（公式推导、手算样例、参数来源）、`docs/接口文档.md`（接口字段、控制范围、WS 事件、错误语义）
2. **公式推导**：每个系数来源、量纲核对必须清晰，写在代码注释或文档中
3. **参数来源**：从 `rules.json`、`equipment_catalog.json`、`design_defaults.py` 或物理常量取值，不能硬编码魔法数字
4. **保持守恒**：冷负荷分项之和恒等于总冷负荷，水系统供回水流量相等，能量平衡必须闭合
5. **降级透明**：额定参数降级来源（目录 → 用户声明 → 默认回退 → 缺失）必须通过 API 显式返回，不能静默回退

### 修改 simcore 时

- **不动点一致性**：稳态是动态核的不动点，任何物理模型修改必须同时验证稳态和实时结果
- **固定迭代次数**：不使用残差判据，保证每 tick 耗时可预测
- **单一状态源**：状态只在 session 中持有，REST/WS 只读投影，前端不计算
- **手动控制语义**：稳态求解仍保持手动控制语义（设备按用户声明的启停和转速运行），实时默认启用自动调节

### 修改设备目录时

- `equipment_catalog.json` 格式：每个设备类型包含 `name`（型号）、`capacity_kw`/`flow_m3_h`/`head_m` 等额定参数、`systems`（支持系统）、`role`（设备角色）
- `name="*"` 是默认回退行，不参与设备选型候选
- 新增设备类型必须同步更新 `DeviceRole`、设备知识库、选型逻辑

### 测试要求

- 新增算法必须写单元测试，包含手算样例对比
- 修改物理模型必须写回归测试，钉死期望值（见 `tests/test_coil_latent.py`、`tests/test_cooling_tower_direction.py`）
- 实时仿真修改必须验证温度、湿度、流量守恒
- 使用 pytest fixture（`conftest.py`）复用测试数据

### 代码风格

- 使用类型标注（mypy 检查通过）
- 函数命名清晰（`calculate_cooling_load`、`allocate_branch_flows`）
- 复杂公式写推导注释，注明量纲
- 避免深层嵌套，提取命名良好的子函数
- 错误消息教学化，指出整改方向（见 `topology.py` Issue 码）

## 关键文件速查

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI 应用，全部路由定义 |
| `app/schemas.py` | 核心契约，Pydantic 模型 |
| `app/simcore/session.py` | 仿真会话主循环 |
| `app/calculators/load.py` | 冷热负荷计算 |
| `app/calculators/hydraulic_network.py` | 多支路水力计算 |
| `app/equipment_selection.py` | 教学设备选型 |
| `app/equipment_knowledge.py` | 设备参数知识 |
| `app/realtime.py` | 实时仿真 REST API |
| `app/data/equipment_catalog.json` | 设备参数表 |
| `app/data/rules.json` | 规则库 |
| `docs/项目文档.md` | 模块结构与算法详解 |
| `docs/算法深读.md` | 公式推导与手算样例 |
| `docs/接口文档.md` | 接口字段与错误语义 |

## 已知缺陷与修复记录

见用户记忆：
- simcore 潜热拆分不守恒（干盘管假除湿，湿盘管多除 16.7%）—— 已修复
- 冷却塔进出水温反置（leaving = entering + rise）—— 已修复
- 末端换热与供水温几乎无关（4°C 和 12°C 出水算出同一冷量）—— **已修复**，
  根因确认：旧式 `capacity·sqrt(flow_ratio)·clamp(ΔT/12,0,1)` 的温差项在
  ΔT>=12 K 时被钳到 1.0，而冷冻水实际温差约 19 K（室温 26、供水 7），恒在
  饱和区，供水温完全不参与计算。已改为 ε-NTU 逆流换热器模型，见
  `app/simcore/devices.py` 的 `terminal_state` 与 `tests/test_coil_ntu.py`
- 热泵制冷读的是制热额定（目录 eer/cooling_capacity_kw 被解析层丢弃）—— 已修复
- 水泵扬程改用闭合环路求和（单程遗漏）—— 已修复
- 冷机 COP 在低负荷率被钳位饱和（部分负荷抛物线无下界，PLR<0.2 时
  `plr_factor` 崩到 0.03，COP 被 `_clamp(cop,1.5,12)` 的 1.5 下界钉死，
  冷凝温度随之失去影响，教学场景"冷却效果不足"算不出 COP 下降）—— **已修复**，
  新增 `PLR_FACTOR_FLOOR = 0.45` 并加全负荷区间回归测试

### 盘管 ε-NTU 模型的已知边界（非缺陷，但必须知情）

- **目录不记录额定工况**。`COIL_RATED_AIR_TEMP_C = 27.0` / `COIL_RATED_WATER_TEMP_C = 7.0`
  是由目录数据反解 + ε<1 热力学硬约束推出的，不是目录声明值。改这个数会
  整体缩放所有非额定工况的出力，属高影响参数，改动需同步复核回归期望值。
- **约 25/64 个目录末端无法标定**（全部 10 个新风机组、15 个 8 排 AHU）：
  其目录容量超过单程盘管 `C_min·ΔT` 上限，ε 需 1.8~2.2。这类末端
  `TerminalState.coil_calibration_limited` 为 `True`，实际按上限出力。
  按「降级透明」约定显式暴露，不得改回静默钳位。
- **`SENSIBLE_HEAT_RATIO = 0.75` 是常量而非按型号取值**。实际显热比随 AHU
  排数在 0.845/0.730/0.658 间变化，0.75 是折中值，也是上述 8 排 AHU 越界的
  成因之一。详见 `docs/设备样本参数.md` 第 338-343 行。

## 参考资源

- GB 50189：公共建筑节能设计标准
- 民用建筑暖通空调设计手册
- Magnus-Tetens 饱和水蒸气压公式
- Hardy-Cross 管网平差算法
