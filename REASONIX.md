# Trace_NL 项目智能体引导

## 技能自动调用规则

以下场景出现时，**必须先调用对应技能**，禁止跳过直接自行处理：

### 调试类
| 触发条件 | 必须调用技能 | 原因 |
|---|---|---|
| 用户报告 bug / 异常行为 / 匹配错误 | `systematic-debugging` | 先建假设再验证，避免盲目修改 |
| 匹配着色不对称 (GREEN 单侧缺失) | `systematic-debugging` | 需系统追踪数据流定位根因 |
| Excel 输出不符合预期 | `systematic-debugging` | 优先检查中间数据而非改代码 |

### 开发类
| 触发条件 | 必须调用技能 | 原因 |
|---|---|---|
| 修改 text_matcher.py 的分类/阈值 | `test-driven-development` | 匹配算法改动必须验证回归 |
| 修改 PDF 解析或内容提取 | `test-driven-development` | 需确保不影响下游矩阵构建 |
| 实现新功能 / 重构模块 | `brainstorming` → `writing-plans` | 先设计方案再执行，避免返工 |

### 验证类
| 触发条件 | 必须调用技能 | 原因 |
|---|---|---|
| 任何代码修改完成后 | `verification-before-completion` | 确保改动有效、无回归 |
| 生成新的追踪矩阵后 | `verification-before-completion` | 比对基准数据、检查统计 |
| 多文件修改结束后 | `review` | 代码审查，发现问题 |

### 知识管理类
| 触发条件 | 必须调用技能 | 原因 |
|---|---|---|
| 项目文件有实质性变更后 | `graphify` --update | 保持知识图谱同步 |
| 需要保存 Obsidian 笔记时 | `obsidian-markdown` / `obsidian-cli` | 复用已有模板和格式 |
| 发现新的 bug 模式 / 修复经验 | `self-improvement` | 避免下次重复踩坑 |

## 标准工作流

```
用户报bug → systematic-debugging (定位根因)
         → brainstorming (设计方案)
         → writing-plans (制定计划)
         → test-driven-development (实施修改)
         → verification-before-completion (验证修复)
         → review (代码审查)
         → graphify --update (更新知识图谱)
```

## 关键路径速查

| 操作 | 命令/入口 |
|---|---|
| 全流程 E2E 测试 | `python run_e2e_test.py` |
| 只生成逆向矩阵 | `python run_e2e_test.py` (改 main 跳过正向) |
| 只生成正向矩阵 | `python generate_forward_matrix.py` |
| 基线对比 | `python compare_detail.py` / `python compare_row_detail.py` |
| 知识图谱更新 | `/graphify . --update` |
| 知识图谱查询 | `/graphify query "<问题>"` |
| 查看匹配统计 | 运行 E2E 后看 GREEN/BLUE/BLACK 输出 |

## 已知技术债

1. `_build_norm_to_orig_map` 与 `_normalize_for_char_compare` 不匹配 — 需要重写 (~80行)
2. 正向矩阵标题截断仍有正文泄漏（`3.2.1系统与设备分级应当进行分类`）
3. `core/text_matcher.py` 文件过大 (~1700行)，建议拆分
4. 缺少单元测试覆盖
