---
日期: 2026-06-12
文档类型: 项目文档
文档概述: LLaMAR 项目的完整中文介绍 — 一个面向部分可观测环境下多智能体机器人长期规划（Long-Horizon Planning）的 VLM 框架
---

<div align="center">

# LLaMAR

**面向部分可观测环境下多智能体机器人的长期规划框架**

[![项目状态: 活跃](https://www.repostatus.org/badges/latest/active.svg)](https://www.repostatus.org/#active)
[![文档](https://img.shields.io/badge/docs-coming_soon-red.svg)](https://github.com/nsidn98/LLaMAR)
[![许可证: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Arxiv](https://img.shields.io/badge/arXiv-2407.10031-green)](https://arxiv.org/abs/2407.10031)
[![项目网站](https://img.shields.io/badge/Project-Website-blue)](https://nsidn98.github.io/LLaMAR/)

</div>

一个基于视觉语言模型（VLM）的模块化框架，用于在部分可观测环境中实现多智能体机器人的长期规划（Long-Horizon Planning）。这是下述论文的官方实现：

**[《Long-Horizon Planning for Multi-Agent Robots in Partially Observable Environments》](https://arxiv.org/abs/2407.10031)**

如果代码有任何不工作的地方，请告知我们，欢迎通过[新建 Issue](https://github.com/nsidn98/LLaMAR/issues) 提出任何问题。注意，我们正在持续改进代码库，使其更易访问和使用。

## 摘要

语言模型（LM）理解自然语言的能力使其成为将人类指令解析为自主机器人任务规划的强大工具。与传统依赖领域知识和人工规则的规划方法不同，LM 能从多样化数据中泛化，以最小的调优适应各种任务，充当压缩的知识库。然而，标准形式的 LM 在面对长期任务时存在挑战，特别是在部分可观测的多智能体环境中。我们提出了基于 LM 的多智能体机器人长期规划框架（LLaMAR），一种面向规划的认知架构，在部分可观测环境中的长期任务上取得了最先进的结果。LLaMAR 采用"规划-执行-纠正-验证"（Plan-Act-Correct-Verify）框架，能够根据动作执行反馈进行自我修正，无需依赖外部 oracle 或模拟器。此外，我们还提出了 MAP-THOR，一个包含 AI2-THOR 环境中不同复杂度家务任务的综合测试套件。实验表明，LLaMAR 在 MAP-THOR 和搜索与救援（Search & Rescue）任务中，成功率比其他最先进的基于 LM 的多智能体规划器高出 30%。

![LLaMAR 架构图](https://raw.githubusercontent.com/nsidn98/nsidn98.github.io/master/files/Publications_assets/LLaMAR/LLaMAR_arch.png)

**LLaMAR 概览**：LLaMAR 将规划过程拆分为 4 个子模块：

- **规划器模块（Planner）** — 将高级语言指令分解为结构化的子任务以供执行。
- **执行器模块（Actor）** — 基于观测和记忆为每个智能体选择高级动作。
- **纠正器模块（Corrector）** — 识别执行中的失败并提出纠正措施以提高任务完成率。
- **验证器模块（Verifier）** — 确认子任务完成情况并更新进度，减少对外部验证的依赖。

这个迭代的"规划-执行-纠正-验证"循环有助于在部分可观测环境中进行长期多智能体规划。

## 使用方法

将您的 OpenAI API 密钥保存在一个名为 `openai_key.json` 的 JSON 文件中，格式如下：

```json
{ "my_openai_api_key": "<your_api_key>" }
```

然后执行 Python 文件：

- **MAP-THOR**

```bash
python AI2Thor/baselines/llamar/llamar.py --task=0 --floorplan=0
```

这将在 `task=0`（将面包、生菜和番茄放入冰箱）和 `floorplan=0`（FloorPlan1）上运行一集。

关于任务和楼层平面的映射，请参考 `configs/config_type1.json`。

或者

- **搜索与救援（Search & Rescue）**

```bash
python SAR/baselines/llamar.py --scene=1 --name='llamar_SAR' --agents=2 --seed=0
```

这将在其中一个火源有 2 个初始照明单元格、另一个火源有 1 个初始照明位置的场景中运行一集。更多场景描述请参考 `SAR/Scenes`。

注意：论文中的实验使用的是 `gpt-4-vision-preview`，该模型已[被弃用](https://platform.openai.com/docs/deprecations)。OpenAI [建议](https://platform.openai.com/docs/guides/vision?lang=node)使用 `gpt-4o-mini`。论文中的部分数字可能无法完全复现。

警告：运行论文中所有实验的 token 费用约为 $3000（取决于实验时 GPT-4 的定价结构）。

## 仓库结构

```
├── README.md               # 项目文档（英文）
├── README_mapthor.md       # MAPTHOR 文档
├── requirements.txt        # 依赖和包要求
├── AI2Thor/                # 与 AI2THOR 实验相关的所有内容
├── SAR/                    # 与 SAR 实验相关的所有内容
├── configs/                # 不同 MAPTHOR 任务的配置文件
├── plots/                  # 生成论文中图表/表格的代码
├── thortils/               # AI2THOR 的一些工具函数
├── vlms/                   # 开源 VLM
├── init_maker/             # 创建新场景初始化的代码
├── results/                # VLM 的一些日志/输出
└── .gitignore              # 版本控制中忽略的文件
```

## 依赖

其他库请参考 `requirements.txt`。

- `pip install openai==0.27.4`
- `pip install ai2thor==5.0.0`
- `sentence-transformers==2.3.1`
- `transformers==4.38.0`
- `pip install open3d==0.16.1`
- `pip install opencv-python==4.7.0.72`

## 问题/请求

如果您对代码或论文有任何问题或请求，请提交 Issue。

## 引用

如果您在研究中使用本代码库，请考虑引用：

```bibtex
@inproceedings{llamar,
  title={Long-Horizon Planning for Multi-Agent Robots in Partially Observable Environments},
  author={Nayak, Siddharth and Orozco, Adelmo Morrison and Ten Have, Marina and Zhang, Jackson and Thirumalai, Vittal and Chen, Darren and Kapoor, Aditya and Robinson, Eric and Gopalakrishnan, Karthik and Harrison, James and Ichter, Brian and Mahajan, Anuj and Balakrishnan Hamsa},
  booktitle={The Thirty-eighth Annual Conference on Neural Information Processing Systems}
}
```

```bibtex
@inproceedings{
nayak2024mapthor,
title={{MAP}-{THOR}: Benchmarking Long-Horizon Multi-Agent Planning Frameworks in Partially Observable Environments},
author={Siddharth Nayak and Adelmo Morrison Orozco and Marina Ten Have and Vittal Thirumalai and Jackson Zhang and Darren Chen and Aditya Kapoor and Eric Robinson and Karthik Gopalakrishnan and Brian Ichter and James Harrison and Anuj Mahajan and Hamsa Balakrishnan},
booktitle={Multi-modal Foundation Model meets Embodied AI Workshop @ ICML2024},
year={2024},
url={https://openreview.net/forum?id=ZygZN5egzy}
}
```

## 贡献

我们欢迎更多 MAP-THOR 场景的贡献，并乐意接受 PR。

## 许可证

MIT 许可证
