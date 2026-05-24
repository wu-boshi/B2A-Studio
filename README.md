# B2A-Studio

**Book-to-Audio Studio** — 本地运行的通用多角色有声书制作工具。上传长篇小说 TXT，自动拆解剧本与演员表，试镜绑定音色，批量合成章节 MP3，并支持内嵌同步歌词 / 导出 LRC。

> 本仓库为独立开源项目，与阶跃星辰（StepFun）无关联。有声书合成需用户自行在 [阶跃星辰 StepPlan 开放平台](https://platform.stepfun.com/step-plan) 订阅并配置个人 API Key。

---

## ⚖️ 法律免责声明（使用前必读）

**【版权与中立性声明】**

1. 本软件属于中立的本地技术辅助工具，本身不存储、不传播、亦不提供任何受著作权法保护的文本或音频内容。
2. 用户上传、导入及处理的所有文本素材，须为用户依法享有著作权、或已取得著作权人合法授权（包括但不限于复制权、改编权、翻译权及汇编权等许可）的内容，或属于已进入公有领域（Public Domain）的公版作品。因用户处理未经授权的侵权作品而导致的一切法律纠纷、侵权责任及损失，均由使用者本人承担全部法律责任，本软件及开发者不承担任何连带或侵权责任。

**【第三方独立性声明】**

3. 本软件为独立开源项目，与上海阶跃星辰智能科技有限公司（包含其关联方，以下统称「阶跃星辰 / StepFun」）不存在任何关联关系或商业合作关系。
4. 本软件仅作为技术接口中转工具，鼓励并引导用户出于个人合规研究或学习目的，自行前往阶跃星辰开放平台（https://platform.stepfun.com/step-plan）自由决定是否订阅 StepPlan 套餐。有声书制作功能完全基于用户在本地输入的个人 API Key，调用由 StepPlan 原生支持的标准化接口实现。

**继续使用本软件即表示您已阅读、理解并同意上述全部条款。**

---

## 功能概览

| 模块 | 说明 |
|------|------|
| 书籍导入 | 本地 `.txt` 上传，章节完整性检查 |
| 剧本智能拆解 | 按章调用大模型生成剧本行、演员表；支持离线 CSV 导入/导出、断点续跑 |
| 配音试镜 | 内置 StepAudio 2.5 官方音色，绑定角色与代表台词 |
| 有声书录制 | 行级 TTS（Step 主引擎 + Edge 应急兜底），自动合拢章节 MP3 |
| 歌词 | 章节 MP3 内嵌 SYLT 同步歌词，并导出同名 `.lrc` |

---

## 技术栈

- **界面**：Streamlit  
- **语言**：Python 3.9+  
- **数据**：SQLite（库存于用户目录 `~/Library/Application Support/B2A-Studio/` 或 `~/.b2a_studio/`）  
- **语音**：StepPlan `stepaudio-2.5-tts`（主）、Edge-TTS（兜底）  
- **音频处理**：pydub、imageio-ffmpeg（内置 ffmpeg）  
- **元数据**：mutagen（MP3 歌词 / LRC）  

---

## 快速开始（推荐：双击运行）

### macOS

1. 安装 [Python 3.9+](https://www.python.org/downloads/)（或使用 Anaconda）。
2. 双击仓库根目录下的 **`打开 B2A-Studio（Mac用户使用）.command`**。  
   - 每次启动会自动执行 `pip install -r requirements.txt` 对齐依赖（含 Streamlit 版本）。  
   - 终端窗口保持打开；关闭窗口即停止服务。  
3. 浏览器将打开 **http://127.0.0.1:8501/**。  
4. 在页面中勾选法律免责声明，在侧边栏填入 **Step API Key** 并保存。

### Windows

1. 安装 [Python 3.9+](https://www.python.org/downloads/)，安装时勾选 **Add Python to PATH**。  
2. 双击 **`打开 B2A-Studio（Windows用户使用）.bat`**。  
   - 每次启动会自动执行 `pip install -r requirements.txt` 对齐依赖。  
   - Streamlit 在后台最小化窗口中运行。  
3. 浏览器将打开 **http://127.0.0.1:8501/**。  
4. 勾选免责声明并配置 API Key。

### 手动安装（全平台）

```bash
cd B2A-Studio
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py --server.port 8501
```

浏览器访问：http://127.0.0.1:8501/

---

## 目录结构

```
.
├── README.md
├── 打开 B2A-Studio（Mac用户使用）.command
├── 打开 B2A-Studio（Windows用户使用）.bat
├── b2a-launch-lib.sh              # macOS 启动共用函数
└── B2A-Studio/
    ├── app.py                     # Streamlit 入口
    ├── pipeline.py                # 全书/按章剧本拆解流水线
    ├── db.py                      # SQLite 持久化
    ├── requirements.txt
    ├── data/                      # 内置 TTS 音色清单等
    ├── logs/                      # 运行日志
    └── utils/                     # 辅助模块（录制、试镜、路径、CSV 等）
        ├── audiobook_*.py
        ├── casting_*.py
        ├── recording_*.py
        └── ...
```

---

## 使用流程

1. **上传小说** `.txt`（≤ 5 MB），勾选法律免责声明。  
2. **剧本智能拆解**：启动全书拆解或断点续跑；亦可导入离线编辑好的剧本 CSV。  
3. **配音试镜**：为 Top 角色与旁白绑定音色。  
4. **有声书录制**：全书或指定章节批量录制，成品位于 `[小说名]_有声书/` 目录。  

---

## 环境变量（可选）

| 变量 | 说明 | 默认 |
|------|------|------|
| `B2A_CHAPTER_COVERAGE_MIN` | 章节剧本覆盖率下限 | `0.98` |
| `B2A_CHAPTER_COVERAGE_MAX` | 章节剧本覆盖率上限 | `1.001` |
| `B2A_PIPELINE_RETRY_WAIT` | 拆解失败后自动重试等待（秒） | `600` |
| `STEP_API_KEY` | 也可写入 `B2A-Studio/.env` | — |

---

## 维护脚本

在 `B2A-Studio` 目录下执行，例如：

```bash
python -m utils.recover_library --csv 剧本_全书.csv --novel "我的小说"
python -m utils.export_casting_backup --title "我的小说"
python -m utils.import_casting_backup --title "我的小说"
```

---

## 本地私有工作区

仓库根目录 `_local/` 已加入 `.gitignore`，用于存放内部测试项目、PRD/SOP、本地小说/剧本/成品等**不参与开源**的内容。开源仓库仅包含 `B2A-Studio/` 工具代码与文档。

首次配置 API Key：复制 `B2A-Studio/.env.example` 为 `B2A-Studio/.env` 并填入密钥。

---

## 常见问题

- **端口 8501 被占用**：关闭已有 B2A-Studio 窗口，或在启动脚本中选择「重新启动」。  
- **Edge-TTS 403**：网络或微软节点波动，稍后断点续录即可；主引擎仍为 Step。  
- **数据库路径**：为避免 iCloud 桌面目录锁库，数据库默认不在项目文件夹内。  

---

## 开源与贡献

欢迎 Issue 与 Pull Request。提交代码前请确保：

- 不在仓库中提交 API Key、`.env` 或用户小说/音频成品。  
- 遵守上文法律免责声明与所在地著作权法规。  

---

## 致谢

- [Streamlit](https://streamlit.io/)  
- [阶跃星辰 StepPlan API](https://platform.stepfun.com/)（用户自行订阅，与本项目无商业关系）  
- [edge-tts](https://github.com/rany2/edge-tts)（应急朗读兜底）  
