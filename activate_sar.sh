#!/usr/bin/env bash
# ── Activate minimal SAR / Integration virtual environment ──
#   用法: source activate_sar.sh
#
#   自动设置:
#     - PYTHONPATH 指向 SAR/ 和 MARoS 本地包
#     - 之后 pytest 自动跳过干扰的 ROS2 插件

LLAMAR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${LLAMAR_ROOT}/.venv/bin/activate"

# ── 设置 Python 模块搜索路径 ──
export PYTHONPATH="${LLAMAR_ROOT}:${LLAMAR_ROOT}/SAR:/home/wyh/daily_work/MARoS/my_a2a/src:/home/wyh/daily_work/MARoS/maros_ws/a2a_lib:${PYTHONPATH}"

# ── pytest 别名 — 自动跳过 ROS2 干扰插件 ──
#   注意: -p no:X 按 entry point 名禁用，不是模块名。ROS2 的 launch_testing_ros
#   entry point 名是 `launch_ros`（不是 `launch_testing_ros_pytest_entrypoint`），
#   用错名字插件不会被禁用，pytest 会在 collection 阶段崩成 PluginValidationError。
alias pytest_sar='python -m pytest -p no:launch_testing -p no:launch_ros -p no:ament_lint -p no:ament_xmllint -p no:ament_pep257 -p no:ament_copyright -p no:ament_flake8'

echo "✅ SAR venv activated  (Python $(python --version 2>&1 | cut -d' ' -f2))"
echo "   Run tests:  pytest_sar integration/test_integration.py -v"
echo "   Run tests:  pytest_sar SAR/core_unittest.py -v"
