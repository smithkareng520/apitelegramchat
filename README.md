# apitelegramchat

Telegram AI assistant with web, workspace, memory, todo, map, and MCP tool support.

## Run locally

```bash
python -m venv .venv
.venv/bin/pip install -e .
export TELEGRAM_BOT_TOKEN=...
export WEBHOOK_TOKEN=...
export WEBHOOK_URL=https://example.com/webhook
export OPENROUTER_API_KEY=...
python -m quart --app apitelegramchat.app:app run --host 0.0.0.0 --port 5000
```

## Docker

```bash
docker build -t apitelegramchat .
docker run --env-file .env -p 5000:5000 apitelegramchat
```

## MCP server

```bash
apitelegramchat-mcp
```

## Optional map integration

Set `AMAP_KEY` to enable Gaode map services. Without it, the standard geographic providers remain available.

## Security verification

Run this in the deployed Linux container after enabling the shell tool:

```bash
python -m apitelegramchat.verify_security
```

## Skill discovery

The MCP server exposes a skill catalog backed by `.claude/skills/*/SKILL.md`. At workspace initialization, the entire project `.claude/skills` tree is mirrored to the workspace `skills/` directory, including scripts and reference files, so the runtime tree stays identical to the packaged project skills.

## Skill runtime dependencies

The bundled `.claude/skills` runtime has project-managed dependencies so the model does not need to install tools at task time.

### Python packages
- `pypdf`, `pdfplumber`, `reportlab`, `pypdfium2`, `pdf2image`, `pytesseract`, `pandas`
- `defusedxml` for DOCX XML handling

### Node.js packages
- Node.js 22
- `docx` 9.7.1 for JavaScript DOCX generation
- `pdf-lib` 1.17.1 and `pdfjs-dist` 6.1.200 for advanced PDF JavaScript workflows

### System tools
- LibreOffice (`soffice`)
- Poppler utilities (`pdftoppm`, `pdftotext`, `pdfimages`)
- `qpdf`
- Pandoc
- ImageMagick (`magick`)
- Tesseract OCR (`tesseract`)

Node dependencies are declared in `package.json`; Python dependencies are declared in both `requirements.txt` and `pyproject.toml`.

## Workspace persistence boundary

Bash/text tools share an ephemeral runtime workspace under `runtime/exec`.
R2 is no longer a mirror of the working directory. `file_editor` persists only
the file it explicitly edits, while Bash-created/modified files must be saved
with the `workspace_commit` tool using explicit file paths. This keeps package
manager outputs such as `node_modules`, virtual environments, caches, and build
trees out of R2 without relying on a growing blacklist.

