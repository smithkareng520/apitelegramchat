# Telegram AI Assistant

**一款基于 Telegram 的多功能 AI 助手**  
📅 最后更新：2025 年 4 月 1 日  

---

## 📋 核心功能

### 🎭 多模态支持
- **文本对话**  
  自然语言交互，支持上下文管理
- **图像生成**  
  使用 `grok-2-image` 模型生成图片
- **文件解析**  
  支持格式：`PDF`/`DOCX`/`TXT`/`JPG`/`PNG`
- **音频转录**  
  处理 `WAV`/`MP3`/`OGG` 格式（需 `gemini-2.5-pro` 模型）
- **批量图片处理**  
  支持一次性上传多张图片

### 🤖 AI 模型管理
- **多模型切换**  
  `/model` 命令动态切换 Grok/DeepSeek/Gemini/Claude 等模型
- **角色扮演系统**  
  `/role` 选择角色：  
  └─ 猫娘 (Neko) | 魅魔 (Succubus) | Isla (Giftia 机器人)

### 🌐 网络增强
- **智能搜索**  
  `/search` 启用 Google 搜索集成
- **内容抓取**  
  非搜索模型通过 Grok 优化网页解析

### ⚙️ 系统管理
- **对话历史控制**  
  自动修剪机制（50 条消息/12 万字符）
  `/clear` 一键清空历史
- **API 监控**  
  `/balance [service]` 查询 DeepSeek/OpenRouter 余额

### ✨ Telegram 增强
- 支持 `HTML` 格式消息（`<b>`/`<i>`/`<pre>`）
- 自动分片超长消息（≤4096 字符）
- 内联键盘快速交互

---

## 🗂 项目结构
```bash
.
├── app.py              # 主应用 (Webhook 处理)
├── utils.py            # 工具函数库
├── ai_handlers.py      # AI 核心逻辑
├── file_handlers.py    # 文件处理器
├── search_engine.py    # 搜索引擎模块
├── config.py           # 配置中心
└── requirements.txt    # 依赖清单
```

## 🚀 部署指南
### 环境要求
```bash
- Python ≥3.10

Telegram Bot Token

API 密钥：
├─ OpenRouter
├─ DeepSeek
├─ Gemini
├─ XAI (Grok)
└─ Google CSE (可选)
```
## **安装流程**
1.克隆仓库
```bash
git clone https://github.com/yourusername/telegram-ai-assistant.git
cd telegram-ai-assistant
```
2.安装依赖

```bash
pip install -r requirements.txt
```
3.配置环境变量
创建 **`.env`** 文件：
```ini
TELEGRAM_BOT_TOKEN=your_token
OPENROUTER_API_KEY=your_key
DEEPSEEK_API_KEY=your_key
GEMINI_API_KEY=your_key
XAI_API_KEY=your_key
WEBHOOK_URL=https://your-domain.com/webhook
WEBHOOK_TOKEN=your_webhook_token
```
4.启动服务

```bash
python app.py
```
### **高级配置**
**Webhook 设置**
```bash
 curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WEBHOOK_URL>?token=<TOKEN>"
```

- **推荐部署平台**

  **`Render` / `Heroku` / `AWS Lambda`**


- **服务监控**

  **建议搭配 UptimeRobot 使用**

## 💡 使用手册

### ⌨️ 基础命令

| 命令                  | 参数                | 功能描述                     |
|-----------------------|---------------------|--------------------------|
| `/start`              | -                   | 激活机器人并显示欢迎信息             |
| `/model`              | `[模型名称]`        | 切换 AI 模型                 |
| `/role`               | `[角色名称]`        | 切换角色人格                   |
| `/search`             | `on`/`off`          | 启用/禁用网络搜索功能              |
| `/clear`              | -                   | 重置当前对话历史                 |
| `/balance`            | `[服务商]`          | 查询 API 余额                |

### 📂 文件操作
- **单文件处理**  
  直接发送以下类型文件：  
  └─ 📄 文档：`PDF`/`DOCX`/`TXT`  
  └─ 🖼️ 图片：`JPG`/`PNG`（支持 OCR）  
  └--🎵 音频：`MP3`/`WAV`/`OGG`（自动转录）

- **批量处理**  
  使用 **媒体组** 同时上传多张图片（上限 10 张）

### ⚠️ 注意事项
1. 模型切换后会自动清空历史对话
2. 音频文件需 ≤15MB，时长 ≤30 分钟
3. 图片生成命令：`/draw [描述文字]`
4. 搜索模式下会消耗 2 倍 Token

## **📦 依赖列表**
```python
# requirements.txt 核心依赖
aiohttp == 3.9.3      # 异步 HTTP 客户端
quart == 0.19.3       # 异步 Web 框架 
PyPDF2 == 3.0.1       # PDF 解析
python-docx == 0.8.11 # DOCX 处理
pytesseract == 0.3.10 # OCR 识别
openai == 1.12.0      # AI 接口
```

---

## **🤝 参与贡献**
1.Fork 项目仓库

2.创建功能分支 (`git checkout -b feature/新功能`)

3.提交代码 (`git commit -am '添加新特性'`)

4.推送分支 (`git push origin feature/新功能`)

5.创建 Pull Request

### **代码规范**

- 遵循 PEP 8 规范

- 新增功能需附带单元测试

- 使用 Type Hint 注解

---

#### 📜 许可证协议：MIT License
