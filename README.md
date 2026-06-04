# FloatTranslate · 悬浮窗屏幕翻译

一个 Windows 桌面悬浮翻译工具。把半透明的捕获窗口对准屏幕上**任何**文字
（游戏、图片、PDF、外文软件界面），即可识别并翻译成中文 —— 哪怕这些文字
本身无法复制。

- **识别**：Windows 系统自带 OCR（`Windows.Media.Ocr`），免安装、本地运行。
- **翻译**：大模型 API，能容忍 OCR 噪声、理解上下文。支持 **Anthropic (Claude)、
  OpenAI、DeepSeek、Google (Gemini)**，在设置里切换。
- **方向**：任意语言 → 简体中文（可在设置里改目标语言）。

## 工作原理

```
透明捕获区  →  mss 截取该区域屏幕  →  Windows OCR 识别文字  →  Claude 翻译  →  显示译文
```

中间的捕获区用 Windows 的 `transparentcolor` 做成真正透明且**鼠标穿透**的
“洞”，所以截图拿到的是底层应用的画面，你也能照常操作下面的程序。

## 安装

需要 **Windows 10/11** 和 **Python 3.x**。

```powershell
pip install -r requirements.txt
```

> OCR 依赖系统已安装的语言包。要识别某种语言，请在
> `设置 → 时间和语言 → 语言和区域` 里添加对应语言（当前已检测到：
> 英语 en-US、简体中文 zh-Hans-CN）。

## 配置 API Key

启动后点 ⚙ 打开设置：

1. 选择**服务商**（Anthropic / OpenAI / DeepSeek / Google）。
2. 填入对应的 **API Key**，点「**验证并获取模型**」—— 会先校验 Key 是否可用，
   再拉取该服务商当前可调用的模型列表填进「模型」下拉框。
3. 选好模型后点保存（写入 `%APPDATA%\FloatTranslate\config.json`）。

每个服务商的 Key 单独保存，可随时切换。留空时会回退到对应环境变量：
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` /
`GOOGLE_API_KEY`（或 `GEMINI_API_KEY`）。

## 运行

```powershell
python app.py
```

或双击 `run.bat`（无控制台窗口）。

## 使用

| 操作 | 说明 |
|------|------|
| 拖动顶部标题栏 | 移动整个窗口 |
| 拖动右下角 ◢ | 调整大小 |
| **翻译** | 立即识别并翻译一次捕获区内容 |
| **自动:开/关** | 定时自动重扫；内容变化时才重新翻译（默认 1.5 秒） |
| **⚙** | 设置：API Key、模型、目标语言、OCR 源语言、自动间隔 |
| **×** | 关闭（自动记住窗口位置和大小） |

## 文件结构

| 文件 | 作用 |
|------|------|
| `app.py` | 悬浮窗主程序与界面 |
| `ocr.py` | Windows OCR 封装 |
| `providers.py` | 各大模型服务商：校验 Key、列模型、翻译 |
| `translator.py` | 翻译入口（带缓存，委托给 provider） |
| `config.py` | 配置读写 |

## 小贴士

- 捕获区**只框住要翻译的文字**，去掉无关画面，OCR 更准。
- 字太小识别差时，把目标程序字号调大或把捕获窗口拉大些。
- 默认模型为 `claude-haiku-4-5`（快且便宜）；要更高翻译质量可在设置里换成
  Sonnet / Opus 系列。
- 译文会缓存：同一段文字不会重复计费。
