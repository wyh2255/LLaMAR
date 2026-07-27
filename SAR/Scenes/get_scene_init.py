"""
场景初始化器工厂模块
====================
根据传入的场景编号 scene_num，动态导入对应的 scene_N.py 模块和 checker.py 模块。
实现将场景编号到具体初始化类的映射，简化场景选择流程。
"""

import os
import importlib.util

def import_module_from_file(file_path: str, module_name: str):
    """
    从指定文件路径动态导入 Python 模块。

    示例:
        mod = import_module_from_file('Tasks/4_clean_floor_kitchen/FloorPlan1.py', 'preinit')
        func = mod.SceneInitializer()  # 使用模块中的类

    参数:
        file_path (str): Python 文件的绝对路径
        module_name (str): 模块名称（用于 importlib 内部标识）

    返回:
        module: 导入的模块对象
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def get_scene_initializer(scene_num : str):
    """
    场景初始化器工厂函数。
    根据场景编号动态导入对应的场景初始化模块和检查器模块。

    参数:
        scene_num (str): 场景编号，例如 "1", "2", "3"

    返回:
        tuple: (scene_initializer, checker)
            - scene_initializer: 场景初始化模块，其中包含 SceneInitializer 类
            - checker:          Checker 模块，其中包含 Checker 类

    注意:
        调用方获取返回后应通过以下方式使用:
            controller, pg_params = scene_initializer.SceneInitializer().preinit(num_agents, agent_names, seed)
            checker = checker.Checker(pg_params)
    # TODO @nsidn98 后续可添加语义映射：任务名 → 文件名
    """
    file_path = os.path.dirname(os.path.realpath(__file__))
    file_path += f"/scene_{scene_num}.py"

    assert os.path.exists(file_path), f'File {file_path} does not exist.'
    checker_file_path = (
        os.path.dirname(os.path.realpath(__file__)) + "/checker.py"
    )

    scene_initializer = import_module_from_file(file_path, "SceneInitializer")
    checker = import_module_from_file(checker_file_path, "Checker")


    # self.controller, pg_params= scene_initializer.SceneInitializer().preinit(
        # num_agents, agent_names, seed
    # )
    # self.checker = checker.Checker(pg_params)

    return scene_initializer, checker
