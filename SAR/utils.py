import matplotlib as mlib
import matplotlib.pyplot as plt
import numpy as np

from core import *

# 代码改编自 https://stackoverflow.com/questions/43971138/python-plotting-colored-grid-based-on-values

def render(objects, title=None, save_path=None, show=True):
    """
    使用 matplotlib 渲染 SAR 网格世界的 2D 可视化。
    不同对象类型用不同颜色区分，火情强度从低到高用渐变色表示。

    颜色编码规则:
        - 白色 (white):       空/默认地面
        - 米色~蓝色渐变:      火情 Flammable 从 NONE->LOW->MEDIUM->HIGH
        - 粉色 (pink):        智能体 (AbsAgent)
        - 深蓝 (cadetblue):   沙源补给站 (Reservoir, type A)
        - 浅蓝 (aqua):        水源补给站 (Reservoir, type B)
        - 黑色 (black):       交付点 (Deposit)
        - 紫色 (purple):      待救援人员 (Person, 未被搬运且未交付)
        - 品红 (magenta):     正在被搬运的人员 (Person, grabbed=True)
        - 棕色 (saddlebrown): A 类火 (Fire type A, 需要沙)
        - 橙红 (salmon):      B 类火 (Fire type B, 需要水)

    参数:
        objects:   场景对象列表（包括 Flammable, AbsAgent, Reservoir, Deposit, Person, Fire 等）
        title:     可选，图表标题
        save_path: 可选，保存路径（如 'output.png'）
        show:      是否调用 plt.show() 显示图形

    返回:
        None（直接绘图或保存到文件）
    """
    # --- 颜色调色板定义 ---
    # 前 6 种（white 到 blue）用于火情强度渐变
    colors=['white', 'beige', 'yellow', 'orange', 'blue', 'brown', 'green', 'purple', 'black', 'pink'] # until 'blue', it's for fire
    colors+=['sandybrown', 'salmon', 'silver', 'cadetblue', 'aqua', 'saddlebrown', 'magenta']
    # 根据颜色名称获取其在列表中的索引（+1e-1 避免 0 值冲突）
    color=lambda c : colors.index(c)+1e-1

    bounds=list(range(len(colors)+1))

    assert len(bounds)-1==len(colors), f"Number of bounds {len(bounds)} should be one greater then amt of colors {len(colors)}"

    # --- 从对象位置构建数据矩阵 ---
    data=np.zeros((Coordinate.HEIGHT, Coordinate.WIDTH))
    for ob in objects:
        # 注意: 这里获取的是 z 轴坐标（在 2D 网格中对应 y 方向）
        xx,yy,zz=ob.position.get()
        if isinstance(ob, Flammable):
            # --- 火情强度：从米色到蓝色渐变（Enum 从 1 开始计数）---
            data[yy][xx]=(ob.intensity.value+1e-1) # Enum is 1-indexed

        elif isinstance(ob, AbsAgent):
            # --- 智能体：粉色 ---
            data[yy][xx]=color('pink')

        elif isinstance(ob, Reservoir):
            # --- 补给站：根据类型改变颜色 ---
            if ob.type=='A':
                # 沙源：深蓝
                data[yy][xx]=color('cadetblue')
            elif ob.type=='B':
                # 水源：浅蓝
                data[yy][xx]=color('aqua')

        elif isinstance(ob, Deposit):
            # --- 交付点：黑色 ---
            data[yy][xx]=color('black')

        elif isinstance(ob, Person):
            # --- 人员：紫色（憋气脸紫的梗）---
            if ob.grabbed:
                # 正在被搬运：品红（表示粉色智能体正携带它）
                data[yy][xx]=color('magenta')
            elif not ob.grabbed and not ob.deposited:
                data[yy][xx]=color('purple')
            else:
                pass  # 已交付的人员不显示

        elif isinstance(ob, Fire):
            # --- 火情对象：根据类型选择颜色 ---
            if ob.fire_type=='A':
                # A 类火（需沙）：沙棕色
                data[yy][xx]=color('saddlebrown')
            elif ob.fire_type=='B':
                # B 类火（需水）：橙红
                data[yy][xx]=color('salmon')
        else:
            # --- 未知类型：白色 ---
            data[yy][xx]=color('white')

    # --- 创建离散颜色映射 ---
    cmap = mlib.colors.ListedColormap(colors)
    norm = mlib.colors.BoundaryNorm(bounds, cmap.N)

    # 启用或禁用图形边框
    plt.figure(frameon=True)

    if title is not None: plt.title(title)

    # --- 隐藏坐标轴标签 ---
    plt.tick_params(bottom=False, top=False, left=False, right=False, labelbottom=False, labelleft=False)

    # --- 显示网格线 ---
    plt.grid(axis='both', color='k', linewidth=2)
    plt.xticks(np.arange(0.5, data.shape[1], 1))  # 调整网格线位置到格子边界
    plt.yticks(np.arange(0.5, data.shape[0], 1))

    # --- 绘制数据矩阵（使用离散颜色映射）---
    plt.imshow(data, cmap=cmap, norm=norm)

    if show:
        # 显示主图
        plt.show()

    if save_path is not None:
        plt.savefig(save_path)
        plt.close()
