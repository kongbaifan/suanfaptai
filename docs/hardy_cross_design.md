# Hardy Cross 方法流量求解器设计文档

## 1. 方法原理

### 1.1 核心思想
Hardy Cross 方法（也称为管网平差法）是一种迭代求解管网流量分配的经典方法，基于两个基本原理：

1. **节点流量守恒**：流入节点的流量 = 流出节点的流量
2. **回路压降守恒**：沿闭合回路一周，总压降为零（不含泵）

### 1.2 压降-流量关系

管道压降与流量的关系可表示为：
```
ΔP = K × Q^n
```

其中：
- `ΔP`：压降（Pa）
- `Q`：流量（m³/h）
- `K`：阻力系数（与管径、长度、摩擦系数相关）
- `n`：流量指数（通常取 1.85 或 2）

**Darcy-Weisbach 公式**（n=2）：
```
ΔP = f × (L/D) × (ρ×v²/2)
K = 8×f×L×ρ / (π²×D^5) × (3600)²  # 单位换算
```

**Hazen-Williams 公式**（n=1.852，适用于水系统）：
```
ΔP = 10.67 × L × Q^1.852 / (C^1.852 × D^4.87)
```

### 1.3 迭代修正公式

对于每个回路，流量修正量为：
```
ΔQ = -Σ(K×|Q|^(n-1)×Q) / (n × Σ(K×|Q|^(n-1)))
```

迭代过程：
```
Q_new = Q_old + ΔQ
```

**物理意义**：
- 分子：回路总压降（带符号）
- 分母：回路阻力导数总和
- 目标：使回路总压降趋近于零

---

## 2. 数据结构设计

### 2.1 管道边（Edge）
```python
@dataclass
class PipeEdge:
    """管道边"""
    id: str                    # 管道或连接ID
    from_node: str            # 起始节点（设备端口）
    to_node: str              # 终止节点（设备端口）
    diameter_mm: float        # 管径（mm）
    length_m: float           # 长度（m）
    roughness_mm: float = 0.05  # 粗糙度（mm，钢管默认0.05）
    flow_m3_h: float = 0.0    # 流量（m³/h，待求解）
    flow_direction: int = 1   # 流向（1=正向，-1=反向）
```

### 2.2 节点（Node）
```python
@dataclass
class NetworkNode:
    """管网节点"""
    id: str                    # 节点ID（设备+端口）
    device_id: str            # 设备ID
    port_type: str            # 端口类型（supply/return）
    external_flow: float = 0.0  # 外部注入流量（源>0，汇<0）
    pressure_pa: float | None = None  # 节点压力（Pa，待求解）
```

### 2.3 回路（Loop）
```python
@dataclass
class Loop:
    """基本回路"""
    id: str                    # 回路ID
    edges: list[str]          # 边ID列表（有序）
    directions: list[int]     # 边方向（1=沿回路方向，-1=逆回路方向）
    correction: float = 0.0   # 流量修正量（m³/h）
```

### 2.4 泵（Pump）
```python
@dataclass
class PumpCurve:
    """泵扬程曲线"""
    device_id: str
    # 二次曲线拟合：H = a - b×Q²
    a: float  # 截距（零流量扬程，m）
    b: float  # 二次系数（m/(m³/h)²）
    rated_flow_m3_h: float  # 额定流量
    rated_head_m: float     # 额定扬程
```

---

## 3. 算法流程

### 3.1 预处理阶段

```python
def preprocess_network(port_graph, medium):
    """预处理管网"""
    # 1. 提取介质子图
    subgraph = port_graph.medium_subgraphs[medium]

    # 2. 构建节点和边列表
    nodes = build_nodes(subgraph)
    edges = build_edges(subgraph)

    # 3. 识别泵设备
    pumps = identify_pumps(subgraph)

    # 4. 检测基本回路（DFS + 回路基）
    loops = detect_loops(nodes, edges)

    # 5. 初始化流量（基于节点平衡的初步估计）
    initialize_flow(nodes, edges)

    return NetworkModel(nodes, edges, loops, pumps)
```

### 3.2 回路检测算法

使用 **DFS + 回边检测** 找到所有基本回路：

```python
def detect_loops(nodes, edges):
    """检测所有基本回路"""
    loops = []
    visited = set()
    path_stack = []

    def dfs(node_id):
        visited.add(node_id)
        path_stack.append(node_id)

        for edge in get_edges_from(node_id):
            neighbor = edge.to_node

            if neighbor in path_stack:
                # 找到回路！
                loop_start = path_stack.index(neighbor)
                loop_nodes = path_stack[loop_start:]
                loops.append(build_loop_from_nodes(loop_nodes, edges))
            elif neighbor not in visited:
                dfs(neighbor)

        path_stack.pop()

    # 从每个未访问节点开始DFS
    for node in nodes:
        if node.id not in visited:
            dfs(node.id)

    return loops
```

### 3.3 Hardy Cross 迭代求解

```python
def solve_hardy_cross(model: NetworkModel, max_iterations=50, tolerance=0.01):
    """Hardy Cross 迭代求解"""
    n = 1.852  # 流量指数（Hazen-Williams）

    for iteration in range(max_iterations):
        max_correction = 0.0

        # 1. 对每个回路计算修正量
        for loop in model.loops:
            numerator = 0.0    # Σ(K×|Q|^(n-1)×Q)
            denominator = 0.0  # n × Σ(K×|Q|^(n-1))

            for edge_id, direction in zip(loop.edges, loop.directions):
                edge = model.edges[edge_id]
                Q = edge.flow_m3_h * direction  # 考虑边方向
                K = calculate_resistance(edge)

                numerator += K * abs(Q)**(n-1) * Q
                denominator += K * abs(Q)**(n-1)

            # 修正量
            if abs(denominator) > 1e-9:
                loop.correction = -numerator / (n * denominator)
                max_correction = max(max_correction, abs(loop.correction))

        # 2. 应用修正量到各边
        for loop in model.loops:
            for edge_id, direction in zip(loop.edges, loop.directions):
                edge = model.edges[edge_id]
                edge.flow_m3_h += loop.correction * direction

        # 3. 检查收敛
        if max_correction < tolerance:
            return SolverResult(
                converged=True,
                iterations=iteration + 1,
                max_residual=max_correction,
                edge_flows={e.id: e.flow_m3_h for e in model.edges.values()}
            )

    # 未收敛
    return SolverResult(
        converged=False,
        iterations=max_iterations,
        max_residual=max_correction,
        edge_flows={e.id: e.flow_m3_h for e in model.edges.values()}
    )
```

### 3.4 阻力系数计算

```python
def calculate_resistance(edge: PipeEdge) -> float:
    """计算管道阻力系数 K（Hazen-Williams）"""
    L = edge.length_m
    D = edge.diameter_mm / 1000  # 转换为米
    C = 120  # Hazen-Williams 粗糙系数（钢管典型值）

    # K = 10.67 × L / (C^1.852 × D^4.87)
    K = 10.67 * L / (C**1.852 * D**4.87)
    return K
```

### 3.5 压力场计算

```python
def calculate_pressure_field(model: NetworkModel, reference_node: str, reference_pressure: float):
    """从参考节点传播压力"""
    model.nodes[reference_node].pressure_pa = reference_pressure
    visited = {reference_node}
    queue = [reference_node]

    while queue:
        current = queue.pop(0)
        P_current = model.nodes[current].pressure_pa

        for edge in get_connected_edges(current):
            neighbor = get_neighbor(edge, current)
            if neighbor not in visited:
                # 计算压降
                Q = edge.flow_m3_h
                K = calculate_resistance(edge)
                delta_P = K * abs(Q)**1.852 * (1 if Q > 0 else -1)

                # 传播压力
                if edge.from_node == current:
                    model.nodes[neighbor].pressure_pa = P_current - delta_P
                else:
                    model.nodes[neighbor].pressure_pa = P_current + delta_P

                visited.add(neighbor)
                queue.append(neighbor)
```

---

## 4. 泵集成

### 4.1 泵曲线建模

```python
def build_pump_curve(device_id: str, catalog_data: dict) -> PumpCurve:
    """从目录数据构建泵曲线"""
    Q_rated = catalog_data.get("flow_m3_h", 50)
    H_rated = catalog_data.get("head_m", 30)

    # 简化假设：零流量扬程 = 1.2 × 额定扬程
    a = H_rated * 1.2
    # 二次系数拟合：使曲线通过额定点
    b = (a - H_rated) / Q_rated**2

    return PumpCurve(device_id, a, b, Q_rated, H_rated)
```

### 4.2 泵的处理

在 Hardy Cross 方法中，泵提供正压头：

```python
# 回路包含泵时，修正分子项
for edge_id, direction in zip(loop.edges, loop.directions):
    if is_pump_edge(edge_id):
        pump = model.pumps[edge_id]
        Q = edge.flow_m3_h
        H_pump = pump.a - pump.b * Q**2
        # 泵提供正压头（负压降）
        numerator -= H_pump * 9810 * direction  # Pa
    else:
        # 管道阻力（正压降）
        # ... 正常计算
```

---

## 5. 实现优先级

### Phase 1: 基础 Hardy Cross（无泵）
- ✅ 数据结构
- ✅ 回路检测
- ✅ 基础迭代求解
- ✅ 阻力系数计算（简化）
- ✅ 单元测试

### Phase 2: 泵集成
- 泵曲线建模
- 泵-管网联合求解
- 泵工作点验证

### Phase 3: 压力场与优化
- 压力场传播
- 数值稳定性优化
- 大型网络性能优化

---

## 6. 测试策略

### 6.1 单元测试用例

**测试 1: 简单单回路**
```
[Pump] → [Pipe1] → [Junction] → [Pipe2] → [Pump]
```
验证：流量守恒、压降平衡、收敛性

**测试 2: 并联管道**
```
        ┌─ [Pipe1] ─┐
[Node1] ┤           ├ [Node2]
        └─ [Pipe2] ─┘
```
验证：流量按阻力反比分配

**测试 3: 多回路网络**
```
[A]─[1]─[B]─[2]─[C]
 │       │       │
[4]     [5]     [6]
 │       │       │
[D]─[7]─[E]─[8]─[F]
```
验证：复杂回路求解、收敛性

**测试 4: 带泵系统**
```
[Pump] → [Pipe1] → [Pipe2] → [Pipe3] → [Pump]
```
验证：泵曲线、工作点、扬程匹配

### 6.2 验收标准

- [ ] 所有单元测试通过
- [ ] 迭代收敛（< 20 次）
- [ ] 流量守恒误差 < 0.1%
- [ ] 压降平衡误差 < 1%
- [ ] 替换现有 `allocate_flow()` 后，集成测试通过

---

## 7. 性能考虑

### 7.1 复杂度
- 回路检测：O(E) 其中 E 是边数
- 每次迭代：O(L×E_loop) 其中 L 是回路数，E_loop 是平均回路边数
- 总时间：O(iter × L × E_loop)

### 7.2 优化策略
- 稀疏矩阵存储
- 只对压降不平衡的回路迭代
- 并行计算独立回路

---

## 8. 参考文献

1. Cross, H. (1936). "Analysis of Flow in Networks of Conduits or Conductors"
2. Rossman, L.A. (2000). "EPANET 2 Users Manual"
3. Jeppson, R.W. (1976). "Analysis of Flow in Pipe Networks"
