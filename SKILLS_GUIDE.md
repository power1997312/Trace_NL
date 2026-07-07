---
project: Trace_NL
archetype: document-analysis
generated: 2026-07-03
tags:
  - skill-guide
  - Trace_NL
---

# Skill Guide for Trace_NL

> 核仪控工程文档追踪验证系统

## 自动激活规则

| 场景 | 触发技能 | 优先级 |
|------|---------|--------|
| 需求不清晰 / 开始新功能 | brainstorming | 高 |
| 设计确认后 | writing-plans -> subagent-driven-development | 高 |
| 编码实现 | test-driven-development | 高 |
| Bug/异常行为 | systematic-debugging | 高 |
| 代码审查 | requesting-code-review | 中 |
| 完成验证 | verification-before-completion | 中 |
| 项目探究/架构理解 | graphify | 低 |
| 重复出现的问题 | self-improving | 低 |

## 项目特定映射

| 项目场景 | 推荐技能 | 原因 |
|---------|---------|------|
| PDF 解析出错 / 文本提取异常 | systematic-debugging | 双引擎解析或清洗流水线问题 |
| 追踪矩阵匹配结果不准 | systematic-debugging + test-driven-development | 先调试阈值/算法，再用 TDD 写回归测试 |
| NLI/Embedding 模型加载失败 | systematic-debugging | 模型权重路径或 CUDA/CPU 回退问题 |
| 新增匹配算法策略 | brainstorming -> writing-plans -> test-driven-development | 设计评审 + 计划 + 测试驱动 |
| 调整 config.py 阈值 | verification-before-completion | 需要验证对基线数据的影响 |
| 新增分析报告 Excel 格式 | writing-plans -> test-driven-development | 设计输出格式 -> 测试驱动生成器 |
| 代码重构 | test-driven-development | 确保重构不改变现有行为 |
| 产出新追踪结果 | graphify -> 复制到 Obsidian vault | 将新 Excel 转为 markdown，更新知识库 |
| 性能优化 | test-driven-development | 先写性能基准测试，再优化 |

## 不适用的技能

| 技能 | 原因 |
|------|------|
| using-git-worktrees | 单项目单分支开发，无需多工作区 |
| dispatching-parallel-agents | 流水线式数据处理，无独立并行子任务 |
