---
description: 通过Atlassian MCP获取Jira任务详情并进行解决方案讨论
allowed-tools: mcp__atlassian__*
argument-hint: "<jira-issue-key> [cloud-id]"
model: claude-3-5-sonnet-20241022
---

# Jira任务助手

我将通过Atlassian MCP帮您获取Jira任务 $ARGUMENTS 的详细信息，并进行分析，然后结合项目代码结构提供解决方案思路。
如果任务是leetcode上的题的话，则不需要考虑项目的代码结构

## 任务获取

让我获取Jira任务 `$1` 的详细信息：

<$1>
<function_calls>
<invoke name="mcp__atlassian__getAccessibleAtlassianResources">
<parameter_name="cloudId">$2</parameter_name>
</invoke>
</function_calls>

<function_calls>
<invoke name="mcp__atlassian__getJiraIssue">
<parameter_name="cloudId">$2</parameter_name>
<parameter_name="issueIdOrKey">$1</parameter_name>
<parameter_name="fields">["summary","description","status","priority","assignee","reporter","created","updated","issuetype","project","labels","components"]</parameter_name>
</invoke>
</function_calls>

## 任务详情分析

基于获取到的Jira任务信息，让我为您分析：

### 📋 任务概况
- **任务编号**: {{issue.key}}
- **标题**: {{issue.fields.summary}}
- **状态**: {{issue.fields.status.name}}
- **优先级**: {{issue.fields.priority.name}}
- **类型**: {{issue.fields.issuetype.name}}
- **项目**: {{issue.fields.project.name}}
- **负责人**: {{issue.fields.assignee.displayName}}
- **报告人**: {{issue.fields.reporter.displayName}}

### 📝 任务描述
```
{{issue.fields.description}}
```

### 🏷️ 标签和组件
- **标签**: {{issue.fields.labels | join(", ")}}
- **组件**: {{issue.fields.components | map(attribute="name") | join(", ")}}

## 代码库分析

让我分析当前项目代码结构，为解决方案提供上下文：

<function_calls>
<invoke name="LS">
<parameter_name="path">/Users/ocean/Documents/Cryptocurrency/jwj_basis</parameter_name>
</invoke>
</function_calls>

<function_calls>
<invoke name="Glob">
<parameter_name="pattern">**/*.py</parameter_name>
<parameter_name="path">/Users/ocean/Documents/Cryptocurrency/jwj_basis</parameter_name>
</invoke>
</function_calls>

## 解决方案建议

基于任务类型和项目结构，我为您提供以下解决方案思路：

### 🎯 核心解决思路

{{"任务类型分析"}}
- 如果 issue.fields.issuetype.name == "Bug":
  - 🔍 **根因分析**: 首先复现问题，定位错误源
  - 🧪 **测试策略**: 编写失败测试用例，验证修复
  - 🔧 **修复方案**: 实施修复并确保不引入新问题
  - ✅ **验证**: 全面测试并准备回归测试

- 如果 issue.fields.issuetype.name == "Story" 或 issue.fields.issuetype.name == "Feature":
  - 📋 **需求澄清**: 确认验收标准和业务价值
  - 🏗️ **技术设计**: 基于现有架构设计解决方案
  - 📝 **实现计划**: 制定分步骤实施计划
  - 🧪 **测试覆盖**: 确保功能测试和集成测试

- 如果 issue.fields.issuetype.name == "Task":
  - 🔍 **任务分解**: 将复杂任务拆分为可管理子任务
  - 📅 **时间规划**: 评估工作量和依赖关系
  - 🔧 **执行策略**: 制定具体实施步骤
  - ✅ **完成标准**: 明确验收条件

### 🏗️ 项目特定建议

基于对jwjbasis项目的分析：

1. **架构考虑**:
   - 项目采用模块化设计，建议在对应模块中进行修改
   - 遵循现有的错误处理和日志记录模式
   - 使用项目中已有的工具类和辅助函数

2. **代码规范**:
   - 遵循项目现有的代码风格和命名约定
   - 添加适当的类型注解和文档字符串
   - 确保新代码通过现有测试套件

3. **测试要求**:
   - 运行 `pytest` 确保不破坏现有功能
   - 为新功能编写相应的测试用例
   - 检查代码覆盖率报告

## 💬 互动讨论

现在让我们深入讨论这个任务的解决方案。请告诉我：

1. **您当前对这个任务的理解是什么？**
2. **有没有特定的技术难点需要解决？**
3. **您希望在哪个方面获得更多指导？**

请随时提问，比如：
- "我应该如何开始实施？"
- "有哪些风险需要注意？"
- "测试策略应该是什么？"
- "如何评估完成时间？"
- "有没有类似的现有代码可以参考？"

我会根据您的具体问题，结合任务详情和项目代码库，提供针对性的建议和解决方案。