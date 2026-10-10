# Skills 依赖清单（轻量、非 OCR）

## 系统包（Dockerfile）
- `libreoffice-writer`：DOCX 修订/转换，以及 XLSX/PPTX 预览、公式重算所需的 LibreOffice 引擎。
- `pandoc`：文档格式转换。
- `poppler-utils`：`pdftotext` / `pdftoppm` 等 PDF 文本提取和页面渲染。
- `qpdf`：PDF 合并、拆分、旋转等操作。
- `fonts-liberation2`、`fonts-dejavu-core`：提供轻量常见西文字体替代；canvas-design 自带 `canvas-fonts/` 中的字体文件。

## Python
- `pypdf`、`pdfplumber`、`pdf2image`、`reportlab`、`defusedxml`、`Pillow`：PDF/DOCX/PPTX 辅助脚本及渲染。
- `openpyxl`、`pandas`：XLSX 编辑、批处理和公式工作簿读写。
- `markitdown[docx,pdf,pptx,xlsx]`：DOCX/PDF/PPTX/XLSX 内容读取。
- 其余项目运行依赖保留在根目录 `requirements.txt` / `pyproject.toml`。

## Node.js
- `docx`：DOCX 生成。
- `pdf-lib`：PDF 生成/编辑。
- `pptxgenjs`：PPTX 生成。
- `react`、`react-dom`、`react-icons`、`sharp`：PPTX 技能中 SVG 图标渲染为 PNG 的流程。

## 明确不安装
- 不安装 `pytesseract`、Tesseract OCR、EasyOCR、PaddleOCR 或其他 OCR 引擎。
- `pdf2image` 只负责把 PDF 页面渲染成图片，不等于 OCR；扫描件仅能转换成图像，不能因此获得可搜索文本。
- 未安装中文字体大包；canvas-design 使用随技能提供的字体。Liberation/DejaVu 是轻量西文字体，不能保证所有中文字符都能显示。
- 为降低镜像体积，移除了 Dockerfile 中未被技能直接要求的 `build-essential`、`cmake`、`ccache` 和 OpenGL 运行库。若某个依赖在目标架构上没有预编译 wheel，pip 安装可能需要重新评估构建工具。
