# 07 WorkBuddy 开放 API 调研：TokenHub 额度接入 Trae 指南

> 调研日期：2026-10-12
> 调研目的：验证"一个 IDE（Trae）+ 两个 Token 池"方案的可行性——日常优先消耗 WorkBuddy 的模型额度，耗尽后再切换回 Trae 自带 Token。

---

## 1. 背景与需求

用户开发工具为 Trae（TraeCode），希望引入腾讯 WorkBuddy 作为第二模型额度来源：

- 只维护一个 IDE，不在两套工具间来回切换；
- 注册 WorkBuddy 账号获取其模型额度（Token 套餐）；
- 在 Trae 中配置 WorkBuddy 侧的 API，日常先用 WorkBuddy 额度，不够用时切回 Trae Token。

**核心卡点**：Token 绑定账号而非 IDE，方案成立与否取决于 WorkBuddy 是否开放 API Key 供外部工具调用。

## 2. 调研结论

**结论：可行。** WorkBuddy 背后的模型服务是腾讯云「大模型服务平台 TokenHub」，TokenHub 提供开放的 API Key 管理入口，接口为 OpenAI 兼容格式，可被 Trae 等外部工具直接调用。

### 关键事实

| 项目 | 内容 |
| :--- | :--- |
| WorkBuddy 定位 | 腾讯推出的全场景桌面 AI 办公 Agent（官网 workbuddy.cn / codebuddy.cn/work） |
| 商业化底座 | 腾讯云大模型服务平台 **TokenHub**（按 Token 套餐计费） |
| API Key 入口 | 腾讯云控制台 → TokenHub → **API Key 管理** → 创建 API Key |
| 接口协议 | **OpenAI 兼容**（`/v1/chat/completions`） |
| 国际站端点示例 | `https://tokenhub-intl.tencentcloudmaas.com/v1/chat/completions` |
| 国内 Key 获取参考 | `copilot.tencent.com/profile/`（CodeBuddy 中国版）及腾讯云 TokenHub 控制台 |
| 额度互通设计 | 官方文档提及 **Token Plan 专属 API Key** 与「通用 Token Plan」配置方式，套餐额度本身即为外部 IDE/CLI 调用而设计 |

## 3. 调研过程（方法留痕）

1. **网络检索**：分别以「WorkBuddy API Key 开放平台」与「Trae WorkBuddy 是什么」两组关键词搜索，确认 WorkBuddy 为腾讯旗下 AI 办公工作台，且腾讯云国际站存在 WorkBuddy 集成文档。
2. **官方文档抓取**：
   - 腾讯云国际站《WorkBuddy》文档：明确记载从 TokenHub 控制台创建 API Key、并在 WorkBuddy 内以「自定义模型」方式配置 `deepseek-v4-pro` 等模型的完整流程（含端点地址）。
   - 腾讯云开发者社区实战文章：验证 WorkBuddy 支持 OpenAI Compatible 方式接入第三方厂商 API（如 NVIDIA NIM、阿里百炼），佐证其自定义模型配置通道成熟。
3. **国内入口补充检索**：定位到腾讯云国内文档《大模型服务平台 TokenHub - CodeBuddy Code/CodeBuddy》，确认国内站同样提供「创建 API Key」流程，并提及 Token Plan 专属 Key 的用法。

## 4. 落地步骤（Trae 侧接入）

1. 注册 WorkBuddy 账号，领取或购买 Token 套餐；
2. 登录腾讯云控制台 → TokenHub → API Key 管理 → **创建 API Key**：
   - 「可访问范围」若选择"限定范围"，务必勾选目标模型；
   - 创建后立即复制保存（部分平台仅展示一次）；
3. Trae 中「添加自定义模型」：
   - Provider：OpenAI 兼容 / Custom；
   - Endpoint：TokenHub 端点（见上表，模型 ID 以 TokenHub 模型列表为准）；
   - API Key：粘贴第 2 步生成的 Key；
   - Model Name：填实际模型 ID（不支持自定义命名）；
4. 发送测试消息验证连通；
5. **去 TokenHub 控制台确认额度扣减**，验证「WorkBuddy 套餐额度 = API Key 额度」为同一池子；
6. 日常使用：Trae 模型切换器中优先选 TokenHub 模型，额度耗尽后切回 Trae 内置模型。

## 5. 注意事项与风险

- **额度互通需实测**：文档口径上 TokenHub 是 WorkBuddy 的商业化底座，大概率同一池子，但以创建 Key 后的实际扣减记录为准；
- **并非全部模型开放直调**：可 API 调用的模型范围以 TokenHub 模型列表为准，WorkBuddy 客户端内可见的模型不一定都能直调；
- **合规提醒**：跨工具调用前建议查阅双方服务条款，确认允许此类用法，避免账号风险；
- **Key 保管**：API Key 等同额度凭证，勿泄露、勿提交进 Git 仓库；
- **能力边界**：Token 池合并省的是钱，不改变上下文长度与模型质量上限。

## 6. 参考链接

- [腾讯云国际站 - WorkBuddy 文档（API Key 获取与配置）](https://intl-sg.tencent-cloud.com/ind/document/product/1300/80640)
- [腾讯云 - 大模型服务平台 TokenHub（CodeBuddy Code 接入）](https://cloud.tencent.com/document/product/1823/131901)
- [腾讯云 - TokenHub（Token Plan 通用配置）](https://cloud.tencent.cn/document/product/1823/130068)
- [WorkBuddy 官方文档 - 模型配置](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Model)
- [CodeBuddy Agent SDK - API Key 获取地址](https://www.codebuddy.ai/docs/zh/cli/sdk)
- [腾讯云开发者社区 - WorkBuddy 接入免费模型实战](https://cloud.tencent.com/developer/article/2660012)
