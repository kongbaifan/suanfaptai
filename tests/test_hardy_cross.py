"""Hardy Cross 流量求解器 - 单元测试

测试回路检测、阻力计算、迭代求解的正确性。
"""

from app.hardy_cross.loop_detector import detect_loops
from app.hardy_cross.models import (
    NetworkModel,
    NetworkNode,
    PipeEdge,
    SolverConfig,
)
from app.hardy_cross.resistance import calculate_resistance_coefficient
from app.hardy_cross.solver import solve_hardy_cross


class TestLoopDetection:
    """回路检测测试"""

    def test_simple_single_loop(self):
        """测试简单单回路检测"""
        # 构建简单方形回路：A → B → C → D → A
        model = NetworkModel(
            nodes={
                "A": NetworkNode("A", "dev1", "supply"),
                "B": NetworkNode("B", "dev2", "supply"),
                "C": NetworkNode("C", "dev3", "supply"),
                "D": NetworkNode("D", "dev4", "supply"),
            },
            edges={
                "AB": PipeEdge("AB", "A", "B", 100, 10),
                "BC": PipeEdge("BC", "B", "C", 100, 10),
                "CD": PipeEdge("CD", "C", "D", 100, 10),
                "DA": PipeEdge("DA", "D", "A", 100, 10),
            },
            loops=[],
        )

        loops = detect_loops(model)

        assert len(loops) == 1, "应该检测到1个回路"
        assert len(loops[0].edges) == 4, "回路应包含4条边"
        assert set(loops[0].edges) == {"AB", "BC", "CD", "DA"}

    def test_two_loops(self):
        """测试两个共享边的回路"""
        # 构建"8"字形：两个方形共享一条边
        #   A - B
        #   |   |
        #   D - C - E
        #       |   |
        #       F - G
        model = NetworkModel(
            nodes={
                "A": NetworkNode("A", "dev1", "supply"),
                "B": NetworkNode("B", "dev2", "supply"),
                "C": NetworkNode("C", "dev3", "supply"),
                "D": NetworkNode("D", "dev4", "supply"),
                "E": NetworkNode("E", "dev5", "supply"),
                "F": NetworkNode("F", "dev6", "supply"),
                "G": NetworkNode("G", "dev7", "supply"),
            },
            edges={
                "AB": PipeEdge("AB", "A", "B", 100, 10),
                "BC": PipeEdge("BC", "B", "C", 100, 10),
                "CD": PipeEdge("CD", "C", "D", 100, 10),
                "DA": PipeEdge("DA", "D", "A", 100, 10),
                "CE": PipeEdge("CE", "C", "E", 100, 10),
                "EG": PipeEdge("EG", "E", "G", 100, 10),
                "GF": PipeEdge("GF", "G", "F", 100, 10),
                "FC": PipeEdge("FC", "F", "C", 100, 10),
            },
            loops=[],
        )

        loops = detect_loops(model)

        assert len(loops) >= 2, "应该检测到至少2个回路"

    def test_parallel_pipes(self):
        """测试并联管道（形成回路）"""
        # A --[Pipe1]--> B
        #  \--[Pipe2]--/
        model = NetworkModel(
            nodes={
                "A": NetworkNode("A", "dev1", "supply"),
                "B": NetworkNode("B", "dev2", "supply"),
            },
            edges={
                "Pipe1": PipeEdge("Pipe1", "A", "B", 100, 10),
                "Pipe2": PipeEdge("Pipe2", "A", "B", 150, 10),
            },
            loops=[],
        )

        loops = detect_loops(model)

        assert len(loops) == 1, "并联管道应形成1个回路"
        assert set(loops[0].edges) == {"Pipe1", "Pipe2"}

    def test_no_loops(self):
        """测试无回路（树状结构）"""
        # A → B → C
        model = NetworkModel(
            nodes={
                "A": NetworkNode("A", "dev1", "supply"),
                "B": NetworkNode("B", "dev2", "supply"),
                "C": NetworkNode("C", "dev3", "supply"),
            },
            edges={
                "AB": PipeEdge("AB", "A", "B", 100, 10),
                "BC": PipeEdge("BC", "B", "C", 100, 10),
            },
            loops=[],
        )

        loops = detect_loops(model)

        assert len(loops) == 0, "树状结构不应有回路"


class TestResistanceCalculation:
    """阻力系数计算测试"""

    def test_resistance_coefficient(self):
        """测试阻力系数计算"""
        edge = PipeEdge("test", "A", "B", diameter_mm=100, length_m=10)
        config = SolverConfig(hazen_williams_C=120)

        K = calculate_resistance_coefficient(edge, config)

        assert K > 0, "阻力系数应为正值"
        assert isinstance(K, float), "阻力系数应为浮点数"

    def test_larger_diameter_lower_resistance(self):
        """测试管径越大，阻力系数越小"""
        edge_small = PipeEdge("small", "A", "B", diameter_mm=50, length_m=10)
        edge_large = PipeEdge("large", "A", "B", diameter_mm=150, length_m=10)
        config = SolverConfig()

        K_small = calculate_resistance_coefficient(edge_small, config)
        K_large = calculate_resistance_coefficient(edge_large, config)

        assert K_small > K_large, "小管径阻力系数应大于大管径"

    def test_longer_pipe_higher_resistance(self):
        """测试管道越长，阻力系数越大"""
        edge_short = PipeEdge("short", "A", "B", diameter_mm=100, length_m=5)
        edge_long = PipeEdge("long", "A", "B", diameter_mm=100, length_m=20)
        config = SolverConfig()

        K_short = calculate_resistance_coefficient(edge_short, config)
        K_long = calculate_resistance_coefficient(edge_long, config)

        assert K_long > K_short, "长管道阻力系数应大于短管道"


class TestHardyCrossSolver:
    """Hardy Cross 求解器测试"""

    def test_simple_loop_convergence(self):
        """测试简单回路收敛性"""
        # 构建简单方形回路，A点注入流量，C点流出
        model = NetworkModel(
            nodes={
                "A": NetworkNode("A", "pump", "outlet", external_flow_m3_h=10.0),
                "B": NetworkNode("B", "junction", "pass"),
                "C": NetworkNode("C", "load", "inlet", external_flow_m3_h=-10.0),
                "D": NetworkNode("D", "junction", "pass"),
            },
            edges={
                "AB": PipeEdge("AB", "A", "B", 100, 10),
                "BC": PipeEdge("BC", "B", "C", 100, 10),
                "CD": PipeEdge("CD", "C", "D", 100, 10),
                "DA": PipeEdge("DA", "D", "A", 100, 10),
            },
            loops=[],
        )

        # 检测回路
        model.loops = detect_loops(model)
        assert len(model.loops) > 0, "应该检测到回路"

        # 求解
        config = SolverConfig(max_iterations=50, tolerance_m3_h=0.01)
        result = solve_hardy_cross(model, config)

        assert result.converged, "求解器应该收敛"
        assert result.iterations <= 50, "迭代次数应在限制内"
        assert result.max_residual_m3_h < config.tolerance_m3_h, "残差应小于容差"

    def test_parallel_pipes_flow_distribution(self):
        """测试并联管道流量分配"""
        # A --[Pipe1: D=100mm]--> B
        #  \--[Pipe2: D=150mm]--/
        # 粗管应分配更多流量
        model = NetworkModel(
            nodes={
                "A": NetworkNode("A", "pump", "outlet", external_flow_m3_h=20.0),
                "B": NetworkNode("B", "load", "inlet", external_flow_m3_h=-20.0),
            },
            edges={
                "Pipe1": PipeEdge("Pipe1", "A", "B", 100, 10),
                "Pipe2": PipeEdge("Pipe2", "A", "B", 150, 10),
            },
            loops=[],
        )

        model.loops = detect_loops(model)
        config = SolverConfig()
        result = solve_hardy_cross(model, config)

        assert result.converged, "求解器应该收敛"

        flow1 = abs(result.edge_flows["Pipe1"])
        flow2 = abs(result.edge_flows["Pipe2"])

        # 粗管（Pipe2）应分配更多流量
        assert flow2 > flow1, "粗管应分配更多流量"
        # 总流量应守恒
        assert abs(flow1 + flow2 - 20.0) < 0.1, "总流量应守恒"

    def test_symmetric_network(self):
        """测试对称网络的对称解"""
        # 构建完全对称的菱形网络
        #      B
        #     / \
        #    A   D
        #     \ /
        #      C
        # A注入流量，D流出，B和C对称
        model = NetworkModel(
            nodes={
                "A": NetworkNode("A", "pump", "outlet", external_flow_m3_h=20.0),
                "B": NetworkNode("B", "junction", "pass"),
                "C": NetworkNode("C", "junction", "pass"),
                "D": NetworkNode("D", "load", "inlet", external_flow_m3_h=-20.0),
            },
            edges={
                "AB": PipeEdge("AB", "A", "B", 100, 10),
                "AC": PipeEdge("AC", "A", "C", 100, 10),
                "BD": PipeEdge("BD", "B", "D", 100, 10),
                "CD": PipeEdge("CD", "C", "D", 100, 10),
            },
            loops=[],
        )

        model.loops = detect_loops(model)
        config = SolverConfig()
        result = solve_hardy_cross(model, config)

        assert result.converged, "求解器应该收敛"

        # 由于完全对称，AB和AC应有相同流量
        flow_AB = abs(result.edge_flows["AB"])
        flow_AC = abs(result.edge_flows["AC"])
        assert abs(flow_AB - flow_AC) < 0.5, "对称网络应有对称解"

        # BD和CD也应相同
        flow_BD = abs(result.edge_flows["BD"])
        flow_CD = abs(result.edge_flows["CD"])
        assert abs(flow_BD - flow_CD) < 0.5, "对称网络应有对称解"
