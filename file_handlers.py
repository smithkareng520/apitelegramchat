import os
import time
import random
import aiohttp
import asyncio
from PyPDF2 import PdfReader
from docx import Document
from PIL import Image
import pytesseract
from config import TELEGRAM_BOT_TOKEN, BASE_URL

async def get_file_path(file_id: str) -> str:
    """通过 file_id 获取文件的真实路径"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/getFile?file_id={file_id}") as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        return data["result"]["file_path"]
                    else:
                        print(f"[ERROR] 获取文件路径失败: {data.get('description')}")
                        return None
                else:
                    print(f"[ERROR] 获取文件路径失败: {await response.text()}")
                    return None
    except Exception as e:
        print(f"[ERROR] 获取文件路径失败: {str(e)}")
        return None

async def download_file(file_id: str, file_path: str) -> bool:
    """异步下载文件"""
    try:
        file_real_path = await get_file_path(file_id)
        if not file_real_path:
            return False

        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_real_path}") as response:
                if response.status == 200:
                    with open(file_path, "wb") as f:
                        f.write(await response.read())
                    return True
                else:
                    print(f"[ERROR] 文件下载失败: {await response.text()}")
                    return False
    except Exception as e:
        print(f"[ERROR] 文件下载失败: {str(e)}")
        return False

def parse_text_file(file_path: str) -> str:
    """解析文本文件，限制最大解析长度"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            if len(content) > 10000:
                return "[内容过长，已截断]"
            return content
    except Exception as e:
        print(f"[ERROR] 解析文本文件失败: {str(e)}")
        return None

def parse_pdf_file(file_path: str) -> str:
    """解析 PDF 文件"""
    reader = PdfReader(file_path)
    text = ""
    for page in reader.pages:
        text += page.extract_text()
    return text

def parse_docx_file(file_path: str) -> str:
    """解析 DOCX 文件"""
    doc = Document(file_path)
    text = ""
    for para in doc.paragraphs:
        text += para.text + "<br>"
    return text

def parse_image_file(file_path: str) -> str:
    """解析图片文件"""
    print(f"[DEBUG] 解析图片文件: {file_path}")
    image = Image.open(file_path)
    print(f"[DEBUG] 图片格式: {image.format}, 图片模式: {image.mode}")
    return pytesseract.image_to_string(image, lang="chi_sim")

async def parse_file(file_id: str, file_name: str) -> str:
    unique_id = f"{int(time.time())}_{random.randint(1000, 9999)}"
    file_path = f"temp_{file_id}_{unique_id}_{file_name}"

    print(f"[DEBUG] 开始下载文件到: {file_path}")
    if not await download_file(file_id, file_path):
        print(f"[ERROR] 文件下载失败: {file_path}")
        return None

    try:
        print(f"[DEBUG] 开始解析文件: {file_path}")
        if file_name.endswith(".txt"):
            content = parse_text_file(file_path)
        elif file_name.endswith(".pdf"):
            content = parse_pdf_file(file_path)
        elif file_name.endswith(".docx"):
            content = parse_docx_file(file_path)
        elif file_name.endswith((".jpg", ".jpeg", ".png")):
            content = parse_image_file(file_path)
        else:
            print(f"[ERROR] 不支持的文件类型: {file_name}")
            return None
    except Exception as e:
        print(f"[ERROR] 文件解析失败: {str(e)}")
        return None
    finally:
        if os.path.exists(file_path):
            print(f"[DEBUG] 删除文件: {file_path}")
            os.remove(file_path)

    return content