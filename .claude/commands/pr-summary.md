当前分支即将在github上提交PR，根据当前分支的名称，当前分支和main的分支的差异，写PR总结。

**注意**：直接在对话框中输出PR总结内容，不要创建文件。用户会手动复制到GitHub PR页面。

参考以下模板：
1. 本次 PR 的目的（What & Why）
	•	新增：
	•	修复：
	•	优化：
	•	原因：
2.  主要修改内容（Changes）

3. 是否影响以下关键功能？（Impact Check）
    交易执行逻辑
    风控逻辑
    交易池确定逻辑
4. 数据一致性

5. 测试（Testing）
本地测试：
	•	单元测试（贴出相关 case）
	•	模拟下单测试（mock API）
	•	回测验证通过
	•	极端情况 / 异常 case 检查（价格跳变、无 liquidity）

部署测试（如有）：
	•	Dev 环境跑通
	•	日志正常，无 error

6. 风险评估（Risk Assessment）

7. 额外信息（Optional）

8. Reviewer Checklist（供 reviewer 使用）