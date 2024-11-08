import logging
import aiohttp
import asyncio
import os
import re
import sys
import tarfile
import io
import gc  # 导入垃圾回收模块
from functools import wraps
from urllib.parse import urlparse
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters
from telegram.helpers import escape_markdown
import nest_asyncio
from dotenv import load_dotenv
from datetime import datetime, timedelta

# 加载 .env 文件中的环境变量
load_dotenv()

# 允许嵌套事件循环
nest_asyncio.apply()

# 启用日志记录
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 设置 httpx 日志级别为 WARNING，屏蔽 INFO 级别日志
logging.getLogger("httpx").setLevel(logging.WARNING)

# 机器人 Token（请确保安全存储，不要硬编码）
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')  # 确保已设置环境变量

# 管理员用户ID列表，从环境变量读取
ADMIN_USER_IDS = os.getenv('ADMIN_USER_IDS', '')
ADMIN_IDS = [int(uid.strip()) for uid in ADMIN_USER_IDS.split(',') if uid.strip().isdigit()]

# 压缩包的下载链接
ARCHIVE_URL = 'https://github.com/Cats-Team/upstream-artifacts/raw/refs/heads/main/archive.tar.gz'

# 存储 URL 内容，结构为 {类别名称: {url: content, ...}, ...}
url_contents = {}

# 定义有效的类别，包括 'all'
VALID_CATEGORIES = ['content', 'dns', 'all']

# 最大重试次数
MAX_RETRIES = 5
# 重试间隔（秒）
RETRY_INTERVAL = 5

# 最大消息长度
MAX_MESSAGE_LENGTH = 4096
# 最大消息数量
MAX_MESSAGES = 3

# 全局变量
initial_load_time = None  # 首次成功加载规则的时间
last_update_time = None
recent_user_ids = {}

# 尝试导入 psutil，如果未安装则设置为 None
try:
    import psutil
except ImportError:
    psutil = None
    logger.error("psutil 模块未安装，/sysinf 命令将不可用。")

def log_user_command(func):
    """
    装饰器：记录用户命令的详细信息，包括用户ID、群组ID和完整消息。
    并更新最近使用过的用户ID列表。
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global recent_user_ids
        user = update.effective_user
        chat = update.effective_chat
        message = update.message.text if update.message else "无消息内容"
        user_id = user.id if user else "未知用户ID"
        chat_id = chat.id if chat else "未知群组ID"
        logger.info(f"群组ID: {chat_id}, 用户ID: {user_id} 发送了命令: {message}")

        # 更新最近使用过的用户ID列表
        if user_id != "未知用户ID":
            now = datetime.now()
            recent_user_ids[user_id] = now
            # 清理一小时前的用户ID
            cutoff_time = now - timedelta(hours=1)
            uids_to_remove = [uid for uid, t in recent_user_ids.items() if t < cutoff_time]
            for uid in uids_to_remove:
                del recent_user_ids[uid]

        return await func(update, context)
    return wrapper

def is_user_admin(user_id):
    """
    检查用户是否为管理员。
    """
    return user_id in ADMIN_IDS

async def download_archive(session: aiohttp.ClientSession, url: str) -> bytes:
    """
    异步下载压缩包内容，包含重试机制。
    如果连续五次失败，程序将终止。
    """
    retries = 0
    while retries < MAX_RETRIES:
        try:
            async with session.get(url, timeout=15) as response:
                response.raise_for_status()
                data = await response.read()
                logger.info(f"成功下载压缩包: {url}")
                return data
        except Exception as e:
            retries += 1
            logger.error(f"下载 {url} 时出错 (尝试 {retries}/{MAX_RETRIES})：{e}")
            if retries < MAX_RETRIES:
                logger.info(f"等待 {RETRY_INTERVAL} 秒后重试 {url}...")
                await asyncio.sleep(RETRY_INTERVAL)
            else:
                logger.critical(f"连续 {MAX_RETRIES} 次失败，无法下载压缩包 {url}。")
                raise Exception(f"无法下载压缩包 {url}")
    return b""

async def download_and_parse_archive(archive_url: str):
    """
    下载压缩包并解析其中的文件，将内容存储到 `url_contents` 中。
    """
    global url_contents, last_update_time, initial_load_time
    async with aiohttp.ClientSession() as session:
        archive_data = await download_archive(session, archive_url)
        if not archive_data:
            logger.error("未能下载压缩包。")
            raise Exception("未能下载压缩包")

        try:
            # 使用 BytesIO 和 with 语句确保及时释放内存
            with io.BytesIO(archive_data) as archive_io:
                with tarfile.open(fileobj=archive_io, mode='r:gz') as tar:
                    members = tar.getmembers()
                    temp_url_contents = {}
                    for member in members:
                        if member.isfile():
                            file_path = member.name
                            # 期望的文件路径格式：./tmp/content/filename.txt 或 ./tmp/dns/filename.txt
                            parts = file_path.split('/')
                            if len(parts) >= 3:
                                category = parts[2]  # 'content' 或 'dns'
                                filename = parts[-1]
                                # 读取文件内容
                                f = tar.extractfile(member)
                                if f:
                                    lines = f.read().decode('utf-8', errors='ignore').splitlines()
                                    if not lines:
                                        continue
                                    # 第一行是 URL
                                    first_line = lines[0].strip()
                                    url_match = re.match(r'^! url:\s*(\S+)', first_line, re.IGNORECASE)
                                    if url_match:
                                        url = url_match.group(1)
                                        content = '\n'.join(lines[1:])  # 剩余内容
                                        if category not in temp_url_contents:
                                            temp_url_contents[category] = {}
                                        temp_url_contents[category][url] = content
                                        logger.info(f"加载 {category} 类别的 URL: {url}")
                                    else:
                                        logger.warning(f"文件 {file_path} 的第一行未找到 URL 信息。")
                    # 手动删除旧的 url_contents 数据并调用垃圾回收
                    if url_contents:
                        del url_contents
                        gc.collect()
                    # 更新全局 url_contents
                    url_contents = temp_url_contents
                    # 更新最后更新时间
                    last_update_time = datetime.now()
                    # 如果 initial_load_time 尚未设置，则设置为当前时间
                    if initial_load_time is None:
                        initial_load_time = last_update_time
        except tarfile.TarError as e:
            logger.error(f"解压缩包时出错: {e}")
            raise e  # 重新抛出异常
        finally:
            # 手动删除不再需要的变量并调用垃圾回收
            del archive_data
            gc.collect()

def split_message(message: str) -> list:
    """
    将长消息拆分为多个不超过 MAX_MESSAGE_LENGTH 的部分。
    最多返回 MAX_MESSAGES 条消息。
    拆分时仅在双换行符处进行，避免在代码块中间拆分。
    """
    messages = []
    parts = message.split('\n\n')
    current_message = ""
    for part in parts:
        # 计算加入当前部分后的长度
        if len(current_message) + len(part) + 2 > MAX_MESSAGE_LENGTH:
            if current_message:
                messages.append(current_message)
                if len(messages) >= MAX_MESSAGES:
                    messages.append(f"*注意：* 搜索结果过多，仅显示前 {MAX_MESSAGES} 条消息。")
                    break
            current_message = part
        else:
            if current_message:
                current_message += "\n\n" + part
            else:
                current_message = part
    if current_message and len(messages) < MAX_MESSAGES:
        messages.append(current_message)
    return messages

@log_user_command
async def send_help_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    发送帮助信息。
    """
    help_text = (
        "📚 *帮助信息*\n\n"
        "*命令列表：*\n"
        "/start \\- 启动机器人并显示帮助信息\n"
        "/search */类别* `关键词` \\- 搜索指定类别中包含关键词的过滤规则 URL\n"
        "/regex */类别* `正则表达式` \\- 使用正则表达式搜索指定类别中的过滤规则 URL\n"
        "/help \\- 显示帮助信息\n"
        "/info \\- 显示机器人和规则更新的相关信息\n\n"
        "*可用类别：* /content, /dns, /all\n\n"
        "📌 示例：\n"
        "`/search /dns ||iqiyi.com^`\n"
        "`/regex /content ^iqiyi.com$`\n"
        "\n"
        "*注意：* 类别参数是可选的，默认为 /all。"
        "\n\n*V1\\.3\\.1*"
    )
    if update.message:
        messages = split_message(help_text)
        for msg in messages:
            await update.message.reply_text(
                msg,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
    else:
        logger.warning("无法发送帮助信息，因为 update.message 为 None")

@log_user_command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /start 命令，发送帮助信息。
    """
    await send_help_message(update, context)

@log_user_command
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /search 命令，根据类别和关键词搜索包含该关键词的过滤规则 URL。
    并显示匹配行的行号及内容。
    """
    if len(context.args) < 1:
        if update.message:
            await update.message.reply_text('用法：`/search /类别 关键词`。例如：`/search /dns \\|\\|iqiyi\\.com^`\n类别参数是可选的，默认为 /all。', parse_mode=ParseMode.MARKDOWN_V2)
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    # 默认类别
    category = 'all'

    # 检查第一个参数是否为类别
    if context.args[0].startswith('/'):
        category_arg = context.args[0]
        category = category_arg[1:].lower()
        if category not in VALID_CATEGORIES:
            if update.message:
                await update.message.reply_text(f'无效的类别 "{escape_markdown(category_arg, version=2)}"。可用类别：/content, /dns, /all', parse_mode=ParseMode.MARKDOWN_V2)
            else:
                logger.warning("无法发送消息，因为 update.message 为 None")
            return
        # 提取关键词
        keyword = ' '.join(context.args[1:]).lower()
    else:
        # 未指定类别，默认使用 'all'
        keyword = ' '.join(context.args).lower()

    if not keyword:
        if update.message:
            await update.message.reply_text('请提供要搜索的关键词。例如：`/search \\|\\|iqiyi\\.com^`', parse_mode=ParseMode.MARKDOWN_V2)
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    found_results = {}

    # 如果类别是 'all', 遍历所有类别
    categories_to_search = VALID_CATEGORIES[:-1] if category == 'all' else [category]

    for cat in categories_to_search:
        for url, content in url_contents.get(cat, {}).items():
            if not content:
                continue
            lines = content.splitlines()
            matching_lines = []
            for idx, line in enumerate(lines, start=1):
                if keyword in line.lower():
                    matching_lines.append((idx, line.strip()))
            if matching_lines:
                if cat not in found_results:
                    found_results[cat] = {}
                found_results[cat][url] = matching_lines

    if found_results:
        response = f"*搜索类别：*/{escape_markdown(category, version=2)}\n*关键词：*`{escape_markdown(keyword, version=2)}`\n\n"
        for cat, urls in found_results.items():
            response += f"*{escape_markdown(cat, version=2)}*:\n\n"
            for url, matches in urls.items():
                parsed_url = urlparse(url)
                filename = os.path.basename(parsed_url.path)
                escaped_filename = escape_markdown(filename, version=2)
                escaped_url = escape_markdown(url, version=2)
                response += f"• [{escaped_filename}]({escaped_url})\n"
                for line_num, line_content in matches:
                    line_content_escaped = escape_markdown(line_content, version=2)
                    response += f"> *{line_num}:*\n> `{line_content_escaped}`\n\n"
            response += "\n"  # 添加空行以分隔不同类别
    else:
        response = f"在类别 /{escape_markdown(category, version=2)} 中未找到包含关键词 '`{escape_markdown(keyword, version=2)}`' 的过滤规则 URL。"

    # 拆分消息
    messages = split_message(response)

    if messages:
        for msg in messages:
            if update.message:
                await update.message.reply_text(
                    msg,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True
                )
            else:
                logger.warning("无法发送消息，因为 update.message 为 None")

@log_user_command
async def regex_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /regex 命令，根据类别和正则表达式搜索匹配的过滤规则 URL。
    并显示匹配行的行号及内容。
    """
    if len(context.args) < 1:
        if update.message:
            await update.message.reply_text('用法：`/regex /类别 正则表达式`。例如：`/regex /content ^iqiyi\\.com$`\n类别参数是可选的，默认为 /all。', parse_mode=ParseMode.MARKDOWN_V2)
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    # 默认类别
    category = 'all'

    # 检查第一个参数是否为类别
    if context.args[0].startswith('/'):
        category_arg = context.args[0]
        category = category_arg[1:].lower()
        if category not in VALID_CATEGORIES:
            if update.message:
                await update.message.reply_text(f'无效的类别 "{escape_markdown(category_arg, version=2)}"。可用类别：/content, /dns, /all', parse_mode=ParseMode.MARKDOWN_V2)
            else:
                logger.warning("无法发送消息，因为 update.message 为 None")
            return
        # 提取正则表达式
        pattern = ' '.join(context.args[1:])
    else:
        # 未指定类别，默认使用 'all'
        pattern = ' '.join(context.args)

    if not pattern:
        if update.message:
            await update.message.reply_text('请提供要搜索的正则表达式。例如：`/regex ^iqiyi\\.com$`', parse_mode=ParseMode.MARKDOWN_V2)
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        if update.message:
            escaped_error = escape_markdown(str(e), version=2)
            await update.message.reply_text(f'无效的正则表达式：{escaped_error}', parse_mode=ParseMode.MARKDOWN_V2)
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    found_results = {}

    # 如果类别是 'all', 遍历所有类别
    categories_to_search = VALID_CATEGORIES[:-1] if category == 'all' else [category]

    for cat in categories_to_search:
        for url, content in url_contents.get(cat, {}).items():
            if not content:
                continue
            lines = content.splitlines()
            matching_lines = []
            for idx, line in enumerate(lines, start=1):
                if regex.search(line):
                    matching_lines.append((idx, line.strip()))
            if matching_lines:
                if cat not in found_results:
                    found_results[cat] = {}
                found_results[cat][url] = matching_lines

    if found_results:
        escaped_pattern = escape_markdown(pattern, version=2)
        response = f"*正则表达式搜索类别：*/{escape_markdown(category, version=2)}\n*正则表达式：*`{escaped_pattern}`\n\n"
        for cat, urls in found_results.items():
            response += f"*{escape_markdown(cat, version=2)}*:\n\n"
            for url, matches in urls.items():
                parsed_url = urlparse(url)
                filename = os.path.basename(parsed_url.path)
                escaped_filename = escape_markdown(filename, version=2)
                escaped_url = escape_markdown(url, version=2)
                response += f"• [{escaped_filename}]({escaped_url})\n"
                for line_num, line_content in matches:
                    line_content_escaped = escape_markdown(line_content, version=2)
                    response += f"> *{line_num}:*\n> `{line_content_escaped}`\n\n"
            response += "\n"  # 添加空行以分隔不同类别
    else:
        escaped_pattern = escape_markdown(pattern, version=2)
        response = f"在类别 /{escape_markdown(category, version=2)} 中未找到符合正则表达式 '`{escaped_pattern}`' 的过滤规则 URL。"

    # 拆分消息
    messages = split_message(response)

    if messages:
        for msg in messages:
            if update.message:
                await update.message.reply_text(
                    msg,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True
                )
            else:
                logger.warning("无法发送消息，因为 update.message 为 None")

@log_user_command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /help 命令，发送帮助信息。
    """
    await send_help_message(update, context)

@log_user_command
async def xinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /xinfo 和 /info 命令，显示机器人信息和统计数据。
    """
    global initial_load_time, last_update_time, recent_user_ids

    now = datetime.now()

    # 计算下次更新时间
    if last_update_time:
        next_update_time = last_update_time + timedelta(hours=1)
    else:
        next_update_time = '未知'

    # 格式化时间
    initial_load_str = initial_load_time.strftime('%Y-%m-%d %H:%M:%S') if initial_load_time else '未知'
    last_update_str = last_update_time.strftime('%Y-%m-%d %H:%M:%S') if last_update_time else '未知'
    next_update_str = next_update_time.strftime('%Y-%m-%d %H:%M:%S') if isinstance(next_update_time, datetime) else '未知'

    # 获取每个类别的文件数量
    category_counts = {}
    for category in VALID_CATEGORIES[:-1]:  # Exclude 'all'
        category_counts[category] = len(url_contents.get(category, {}))

    # 格式化用户ID
    def format_user_id(user_id):
        uid_str = str(user_id)
        if len(uid_str) <= 4:
            masked_uid = uid_str  # 不遮掩长度小于等于4的ID
        else:
            masked_uid = uid_str[:2] + '*' * (len(uid_str) - 4) + uid_str[-2:]
        # 检查是否为管理员
        if is_user_admin(user_id):
            masked_uid += ' #'  # 添加管理员标记
        return masked_uid

    # 获取一小时内使用过的用户ID列表
    user_ids_within_hour = [uid for uid, t in recent_user_ids.items() if t >= now - timedelta(hours=1)]
    # 格式化并处理用户ID
    formatted_user_ids = []
    for uid in user_ids_within_hour:
        uid_str = format_user_id(uid)
        # 转义反引号
        uid_str_escaped = uid_str.replace('`', '\\`')
        # 使用等宽字体显示
        uid_str_monospace = f'`{uid_str_escaped}`'
        formatted_user_ids.append(uid_str_monospace)

    # 组装响应消息
    response = (
        f"*机器人信息：*\n"
        f"启动时间：{escape_markdown(initial_load_str, version=2)}\n"
        f"上次规则更新时间：{escape_markdown(last_update_str, version=2)}\n"
        f"下次更新时间：{escape_markdown(next_update_str, version=2)}\n"
        f"\n"
        f"*规则文件数量：*\n"
    )
    for category, count in category_counts.items():
        response += f"`{escape_markdown(category, version=2)}：{count}`\n"

    response += "\n"
    response += "*一小时内使用过的用户ID列表：*\n"
    if formatted_user_ids:
        response += '\n'.join(formatted_user_ids)
    else:
        response += '无'

    # 发送响应消息
    if update.message:
        await update.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
    else:
        logger.warning("无法发送消息，因为 update.message 为 None")

@log_user_command
async def fetchnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /fetchnow 命令，强制重新加载规则，但只有管理员用户才能使用。
    """
    user = update.effective_user
    user_id = user.id if user else None
    if not user_id or not is_user_admin(user_id):
        if update.message:
            await update.message.reply_text(
                "抱歉，您没有权限使用此命令。",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    # 重新加载规则
    try:
        await download_and_parse_archive(ARCHIVE_URL)
        if update.message:
            await update.message.reply_text(
                "规则已成功重新加载。",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
    except Exception as e:
        logger.error(f"重新加载规则时出错：{e}")
        if update.message:
            await update.message.reply_text(
                "抱歉，重新加载规则时发生错误。",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")

@log_user_command
async def killnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /killnow 命令，立即退出机器人程序，但只有管理员用户才能使用。
    """
    user = update.effective_user
    user_id = user.id if user else None
    if not user_id or not is_user_admin(user_id):
        if update.message:
            await update.message.reply_text(
                "抱歉，您没有权限使用此命令。",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    if update.message:
        await update.message.reply_text(
            "机器人即将关闭。",
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
    else:
        logger.warning("无法发送消息，因为 update.message 为 None")
    # 停止应用程序
    await context.application.stop()
    sys.exit(0)

@log_user_command
async def sysinf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理 /sysinf 命令，显示当前Linux VPS系统信息，只有管理员用户才能使用。
    """
    user = update.effective_user
    user_id = user.id if user else None
    if not user_id or not is_user_admin(user_id):
        if update.message:
            await update.message.reply_text(
                "抱歉，您没有权限使用此命令。",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    if psutil is None:
        if update.message:
            await update.message.reply_text(
                "psutil 模块未安装，无法获取系统信息。",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
        else:
            logger.warning("无法发送消息，因为 update.message 为 None")
        return

    # 获取系统信息
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_percent = psutil.cpu_percent(interval=1)
    disk = psutil.disk_usage('/')

    # 对变量进行转义
    cpu_percent_escaped = escape_markdown(str(cpu_percent), version=2)
    mem_used_escaped = escape_markdown(psutil._common.bytes2human(mem.used), version=2)
    mem_total_escaped = escape_markdown(psutil._common.bytes2human(mem.total), version=2)
    mem_percent_escaped = escape_markdown(str(mem.percent), version=2)
    swap_used_escaped = escape_markdown(psutil._common.bytes2human(swap.used), version=2)
    swap_total_escaped = escape_markdown(psutil._common.bytes2human(swap.total), version=2)
    swap_percent_escaped = escape_markdown(str(swap.percent), version=2)
    disk_used_escaped = escape_markdown(psutil._common.bytes2human(disk.used), version=2)
    disk_total_escaped = escape_markdown(psutil._common.bytes2human(disk.total), version=2)
    disk_percent_escaped = escape_markdown(str(disk.percent), version=2)

    # 构建响应消息，手动转义静态文本中的特殊字符
    response = (
        f"*系统信息：*\n"
        f"CPU使用率：{cpu_percent_escaped}%\n"
        f"内存使用：{mem_used_escaped} / {mem_total_escaped} "
        f"\\({mem_percent_escaped}%\\)\n"
        f"交换区使用：{swap_used_escaped} / {swap_total_escaped} "
        f"\\({swap_percent_escaped}%\\)\n"
        f"磁盘使用：{disk_used_escaped} / {disk_total_escaped} "
        f"\\({disk_percent_escaped}%\\)\n"
    )

    # 发送响应消息
    if update.message:
        await update.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
    else:
        logger.warning("无法发送消息，因为 update.message 为 None")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    全局错误处理器，记录错误并向用户发送友好提示。
    """
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "抱歉，发生了一个错误。请稍后再试。",
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"无法发送错误消息: {e}")

async def periodic_update(archive_url: str):
    """
    定期更新压缩包内容的任务，每1小时执行一次。
    """
    while True:
        await asyncio.sleep(60 * 60)  # 等待1小时
        logger.info("开始定期更新压缩包...")
        try:
            await download_and_parse_archive(archive_url)
            logger.info("定期更新成功。")
        except Exception as e:
            logger.error(f"定期更新失败：{e}")
            logger.info("将等待1小时后再尝试更新。")

async def main():
    global url_contents, initial_load_time

    # 检查 BOT_TOKEN 是否设置
    if not BOT_TOKEN:
        logger.error("未设置 TELEGRAM_BOT_TOKEN 环境变量。")
        sys.exit(1)

    # 下载并解析压缩包
    logger.info("开始下载并解析压缩包...")
    try:
        await download_and_parse_archive(ARCHIVE_URL)
    except Exception as e:
        logger.critical(f"未能下载压缩包。程序即将终止。错误信息：{e}")
        sys.exit(1)
    logger.info("所有 URL 内容已加载。")

    # 初始化机器人
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 注册命令处理器，支持在群组中非@使用
    application.add_handler(CommandHandler(["start"], start, filters=filters.COMMAND))
    application.add_handler(CommandHandler(["search", "xsearch"], search, filters=filters.COMMAND))
    application.add_handler(CommandHandler(["regex", "xregex"], regex_search, filters=filters.COMMAND))
    application.add_handler(CommandHandler(["help"], help_command, filters=filters.COMMAND))
    application.add_handler(CommandHandler(["xinfo", "info"], xinfo, filters=filters.COMMAND))
    application.add_handler(CommandHandler(["fetchnow"], fetchnow, filters=filters.COMMAND))
    application.add_handler(CommandHandler(["killnow"], killnow, filters=filters.COMMAND))
    application.add_handler(CommandHandler(["sysinf"], sysinf, filters=filters.COMMAND))

    # 注册错误处理器
    application.add_error_handler(error_handler)

    # 启动定期更新任务
    asyncio.create_task(periodic_update(ARCHIVE_URL))

    # 启动机器人
    logger.info("机器人已启动。")
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
